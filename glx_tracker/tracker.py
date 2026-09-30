"""Timer state machine and Hubstaff-style activity measurement.

Once per second (while the timer runs) we record one sample: was there keyboard input, mouse
input, which app is in front and (for browsers) which domain. Samples are grouped into
10-minute blocks aligned to the clock (:00, :10, ...), like Hubstaff.

Idle handling: when there has been no input for ``idle_timeout`` the quiet seconds (going back
to when input stopped) are held back. When the person returns they choose keep / discard /
discard-and-stop. If nobody returns within ``idle_autostop`` the timer stops at the moment the
input stopped. Samples are only turned into blocks once they are older than the idle timeout,
so a later idle decision can still change them.
"""

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from glx_tracker.config import new_client_id
from glx_tracker.platforms.base import Foreground, domain_from_url

log = logging.getLogger("glx_tracker.tracker")

BLOCK = 600
SLEEP_GAP = 30  # a tick arriving this late means the computer slept / froze


def slot_of(ts: int) -> int:
	"""A sample stamped ``ts`` covers the second [ts-1, ts)."""
	return (ts - 1) - (ts - 1) % BLOCK


@dataclass
class Sample:
	ts: int
	kb: bool
	mouse: bool
	app: str | None
	domain: str | None
	kind: str = "work"  # work | idle (undecided) | kept | discarded


@dataclass
class Block:
	slot: int
	tracked: int = 0
	keyboard: int = 0
	mouse: int = 0
	overall: int = 0
	idle_kept: int = 0
	idle_discarded: int = 0
	apps: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))  # app -> [secs, activations]
	urls: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))  # (domain, app) -> [secs, act]

	def event(self, client_id: str) -> dict:
		tools = [{"t": "App", "n": a, "s": v[0], "a": v[1]} for a, v in self.apps.items() if v[0]]
		tools += [{"t": "URL", "n": d, "app": a, "s": v[0], "a": v[1]} for (d, a), v in self.urls.items() if v[0]]
		return {
			"type": "block",
			"client_id": client_id,
			"slot": self.slot,
			"tracked": self.tracked,
			"keyboard": self.keyboard,
			"mouse": self.mouse,
			"overall": self.overall,
			"idle_kept": self.idle_kept,
			"idle_discarded": self.idle_discarded,
			"tools": tools,
		}


@dataclass
class Idle:
	start: int  # first quiet second
	returned: float | None = None  # when input came back (dialog shown)
	reason: str = "idle"  # idle | sleep


class Tracker:
	def __init__(self, platform, outbox, config=None, clock=time.time):
		self.platform = platform
		self.outbox = outbox
		self.config = config
		self.clock = clock
		self.lock = threading.RLock()

		self.idle_timeout = 5 * 60
		self.idle_autostop = 30 * 60
		self.track_apps = True
		self.track_urls = True

		self.state = "stopped"  # stopped | running | paused
		self.session = None  # {"client_id", "task", "subject", "started"}
		self.break_session = None
		self.samples: deque[Sample] = deque()
		self.blocks: dict[int, Block] = {}
		self.idle: Idle | None = None
		self.last_tick = None
		self._prev_app = None
		self._prev_url = None

		self.fg = Foreground(None)
		self._url = (None, None, 0.0)  # (app, domain, when)

		# display helpers
		self.server_today = 0
		self.server_today_date = None
		self.unsynced = 0  # tracked seconds in blocks queued but not yet confirmed by the server

		# callbacks (set by the UI)
		self.on_change = lambda: None
		self.on_idle_return = lambda idle: None
		self.on_notice = lambda text: None

	# ------------------------------------------------------------------ settings
	def apply_settings(self, s: dict | None):
		if not s:
			return
		with self.lock:
			self.idle_timeout = max(60, int(s.get("idle_timeout_min") or 5) * 60)
			self.idle_autostop = max(self.idle_timeout + 60, int(s.get("idle_autostop_min") or 30) * 60)
			self.track_apps = bool(s.get("track_apps", 1))
			self.track_urls = bool(s.get("track_urls", 1))

	# ------------------------------------------------------------------ timer control
	def start(self, task: dict | None = None, ts: float | None = None):
		with self.lock:
			now = int(ts or self.clock())
			if self.state == "running":
				self._stop_work(now, "Switch")
			elif self.state == "paused":
				self._end_break(now)
			self.session = {
				"client_id": new_client_id(),
				"task": (task or {}).get("name"),
				"subject": (task or {}).get("subject") or "No task",
				"project_name": (task or {}).get("project_name"),
				"started": now,
			}
			self.outbox.put({"type": "start", "client_id": self.session["client_id"], "ts": now, "task": self.session["task"], "session_type": "Work"})
			self.state = "running"
			self.last_tick = now
			self._prev_app = self._prev_url = None
			self._persist()
		if self.config:
			self.config.remember_task(task)
		self.on_change()

	def stop(self, reason: str = "User", ts: float | None = None):
		with self.lock:
			now = int(ts or self.clock())
			if self.state == "running":
				self._stop_work(now, reason)
			elif self.state == "paused":
				self._end_break(now)
			self.state = "stopped"
			self.session = None
			self._persist()
		self.on_change()

	def pause(self):
		with self.lock:
			if self.state != "running":
				return
			now = int(self.clock())
			task = {"name": self.session["task"], "subject": self.session["subject"], "project_name": self.session.get("project_name")}
			self._stop_work(now, "User")
			self.break_session = {"client_id": new_client_id(), "started": now, "resume_task": task}
			self.outbox.put({"type": "start", "client_id": self.break_session["client_id"], "ts": now, "session_type": "Break"})
			self.state = "paused"
			self.session = None
			self._persist()
		self.on_change()

	def resume(self):
		with self.lock:
			if self.state != "paused":
				return
			task = (self.break_session or {}).get("resume_task")
		self.start(task if task and task.get("name") else None)

	def _end_break(self, now: int):
		if self.break_session:
			self.outbox.put({"type": "stop", "client_id": self.break_session["client_id"], "ts": now, "reason": "User"})
			self.break_session = None

	def _stop_work(self, now: int, reason: str, drop_from: int | None = None):
		"""Flush everything up to ``now`` and queue the stop.

		``drop_from``: samples from this second on are thrown away (stop at the start of idle time).
		"""
		if self.idle is not None:
			self._resolve_idle_locked("discard")
		cut = now + 1 if drop_from is None else drop_from
		while self.samples and self.samples[-1].ts >= cut:
			self._count_discarded(self.samples.pop())
		self._commit(horizon=None)
		self._finalize_blocks(all_blocks=True)
		self.outbox.put({"type": "stop", "client_id": self.session["client_id"], "ts": now, "reason": reason})
		self.last_tick = None

	# ------------------------------------------------------------------ sampling
	def set_url(self, app: str | None, url: str | None):
		"""Called by the URL watcher thread."""
		self._url = (app, domain_from_url(url), self.clock())

	def tick(self):
		with self.lock:
			if self.state != "running":
				return
			now = int(self.clock())
			if self.last_tick is not None and now <= self.last_tick:
				return
			gap = now - self.last_tick if self.last_tick is not None else 1

			if gap > SLEEP_GAP:
				self._handle_gap(self.last_tick, now)
				if self.state != "running":
					return

			idle_s = self.platform.idle_seconds()
			kb, mouse = self.platform.input_state()
			try:
				self.fg = self.platform.foreground()
			except Exception:
				log.exception("foreground failed")
				self.fg = Foreground(None)

			app = self.fg.app if self.track_apps else None
			domain = None
			if app and self.fg.browser and self.track_urls:
				u_app, u_domain, u_when = self._url
				if u_app == app and now - u_when < 10:
					domain = u_domain

			# fill seconds skipped by a slow tick (1 < gap <= SLEEP_GAP) with quiet samples
			for t in range((self.last_tick or now - 1) + 1, now):
				self.samples.append(Sample(t, False, False, app, domain))
			self.samples.append(Sample(now, kb, mouse, app, domain))
			self.last_tick = now

			active = kb or mouse or idle_s < 1.5
			if self.idle is None:
				if idle_s >= self.idle_timeout:
					self._begin_idle(int(now - idle_s) + 1)
			else:
				if not self.idle.returned:
					if active:
						self.idle.returned = now
						self._mark_idle_until(now)
						self._notify_idle_return()
					else:
						self.samples[-1].kind = "idle"
						if now - self.idle.start >= self.idle_autostop:
							self._auto_stop()
							return
				elif now - self.idle.returned >= self.idle_autostop:
					# nobody answered the idle question for a long time: don't count the idle time
					self._resolve_idle_locked("discard")
					self.on_notice("Idle time was discarded because the question was not answered.")

			self._commit(horizon=self.idle_timeout + 10)
			self._finalize_blocks()
			if now % 30 == 0:
				self._persist()

	def _handle_gap(self, last: int, now: int):
		"""The computer slept (or the app was frozen) between ``last`` and ``now``."""
		if now - last >= self.idle_autostop:
			self._stop_work(last, "Sleep")
			self.state = "stopped"
			self.session = None
			self._persist()
			self.on_notice(f"Timer stopped at {time.strftime('%H:%M', time.localtime(last))} because the computer was asleep.")
			self.on_change()
			return
		# shorter gap: treat it like idle time the person decides about
		for t in range(last + 1, now):
			self.samples.append(Sample(t, False, False, None, None, "idle"))
		if self.idle is None:
			self.idle = Idle(start=last + 1, reason="sleep")
		self.last_tick = now - 1

	def _begin_idle(self, start: int):
		self.idle = Idle(start=start)
		self._mark_idle_until(int(self.clock()) + 1)
		log.info("idle since %s", start)
		self.on_change()

	def _mark_idle_until(self, end: int):
		for s in self.samples:
			if self.idle.start <= s.ts < end and s.kind == "work" and not (s.kb or s.mouse):
				s.kind = "idle"

	def _notify_idle_return(self):
		idle = self.idle
		self.on_change()
		self.on_idle_return(idle)

	def _auto_stop(self):
		start = self.idle.start
		self._stop_work(start, "Idle", drop_from=start)
		self.state = "stopped"
		self.session = None
		self._persist()
		self.on_notice(f"Timer stopped at {time.strftime('%H:%M', time.localtime(start))} because you were away.")
		self.on_change()

	def resolve_idle(self, action: str):
		"""keep | discard | stop   (answer to the idle question)."""
		with self.lock:
			if self.idle is None or self.state != "running":
				return
			if action == "stop":
				self._stop_work(self.idle.start, "Idle", drop_from=self.idle.start)
				self.state = "stopped"
				self.session = None
				self._persist()
			else:
				self._resolve_idle_locked(action)
		self.on_change()

	def _resolve_idle_locked(self, action: str):
		for s in self.samples:
			if s.kind == "idle":
				s.kind = "kept" if action == "keep" else "discarded"
		self.idle = None

	# ------------------------------------------------------------------ blocks
	def _count_discarded(self, s: Sample):
		slot = slot_of(s.ts)
		self.blocks.setdefault(slot, Block(slot)).idle_discarded += 1

	def _commit(self, horizon: int | None):
		limit = None if horizon is None else int(self.clock()) - horizon
		while self.samples:
			s = self.samples[0]
			if limit is not None and s.ts > limit:
				break
			if s.kind == "idle":  # undecided
				if horizon is not None:
					break
				s.kind = "discarded"
			self.samples.popleft()
			slot = slot_of(s.ts)
			b = self.blocks.setdefault(slot, Block(slot))
			if s.kind == "discarded":
				b.idle_discarded += 1
				continue
			b.tracked += 1
			if s.kind == "kept":
				b.idle_kept += 1
			if s.kb:
				b.keyboard += 1
			if s.mouse:
				b.mouse += 1
			if s.kb or s.mouse:
				b.overall += 1
			if s.app:
				rec = b.apps[s.app]
				rec[0] += 1
				if s.app != self._prev_app:
					rec[1] += 1
			self._prev_app = s.app
			if s.domain:
				rec = b.urls[(s.domain, s.app)]
				rec[0] += 1
				if s.domain != self._prev_url:
					rec[1] += 1
			self._prev_url = s.domain

	def _finalize_blocks(self, all_blocks: bool = False):
		if not self.session:
			return
		oldest_open = slot_of(self.samples[0].ts) if self.samples else None
		for slot in sorted(self.blocks):
			if not all_blocks:
				# a block is complete once no uncommitted sample can still fall into it
				if oldest_open is not None and slot >= oldest_open:
					break
				if oldest_open is None and slot + BLOCK > int(self.clock()):
					break
			b = self.blocks.pop(slot)
			if b.tracked or b.idle_discarded:
				self.outbox.put(b.event(self.session["client_id"]))
				self.unsynced += b.tracked

	# ------------------------------------------------------------------ display
	def today_seconds(self) -> int:
		with self.lock:
			pending = sum(b.tracked for b in self.blocks.values())
			pending += sum(1 for s in self.samples if s.kind in ("work", "kept"))
			return self.server_today + self.unsynced + pending

	def session_seconds(self) -> int:
		with self.lock:
			if self.state != "running" or not self.session:
				return 0
			return max(0, int(self.clock()) - self.session["started"])

	def status(self) -> dict:
		with self.lock:
			return {
				"state": self.state,
				"tracking": self.state == "running",
				"client_id": self.session["client_id"] if self.session else None,
				"task": self.session["task"] if self.session else None,
				"subject": self.session["subject"] if self.session else None,
				"since": self.session["started"] if self.session else None,
				"idle": bool(self.idle),
			}

	def abort(self, client_id: str, message: str):
		"""The server refused this timer (e.g. tracking switched off): forget it locally."""
		with self.lock:
			if self.session and self.session["client_id"] == client_id:
				self.samples.clear()
				self.blocks.clear()
				self.idle = None
				self.state = "stopped"
				self.session = None
				self.last_tick = None
				self._persist()
			elif self.break_session and self.break_session["client_id"] == client_id:
				self.break_session = None
				if self.state == "paused":
					self.state = "stopped"
				self._persist()
			else:
				return
		self.on_notice(message)
		self.on_change()

	# ------------------------------------------------------------------ crash safety
	def _persist(self):
		if not self.config:
			return
		self.config.set(
			"running",
			{"session": self.session, "break": self.break_session, "state": self.state, "last_tick": self.last_tick or int(self.clock())}
			if self.state != "stopped"
			else None,
		)

	def recover(self):
		"""After a crash / forced quit: close the timer that was left running."""
		if not self.config:
			return
		info = self.config.get("running")
		if not info:
			return
		last = int(info.get("last_tick") or self.clock())
		if info.get("session"):
			self.outbox.put({"type": "stop", "client_id": info["session"]["client_id"], "ts": last, "reason": "Quit"})
		if info.get("break"):
			self.outbox.put({"type": "stop", "client_id": info["break"]["client_id"], "ts": last, "reason": "Quit"})
		self.config.set("running", None)
		log.info("recovered timer left running at %s", last)
