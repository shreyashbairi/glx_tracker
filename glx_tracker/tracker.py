"""Timer state machine and Hubstaff-style activity measurement.

Once per second (while the timer runs) we record one sample: was there keyboard input, mouse
input, which app is in front and (for browsers) which domain. Samples are grouped into
10-minute blocks aligned to the clock (:00, :10, ...), like Hubstaff.

Idle handling: when there has been no input for ``idle_timeout`` the quiet seconds (going back
to when input stopped) are held back. When the person returns they choose keep / discard /
discard-and-stop. If nobody returns within ``idle_autostop`` the timer stops at the moment the
input stopped. Samples are only turned into blocks once they are older than the idle timeout,
so a later idle decision can still change them.

v0.3.0: the idle question is shown as soon as idle begins (not only on return), so the person
sees that the timer is about to stop and can keep it running (a call, a meeting). And while the
timer is stopped or on a break, someone who is clearly working (input in at least
NUDGE_MIN_ACTIVE of the last NUDGE_WINDOW seconds) gets a popup asking to start / resume the
timer, Monday to Friday 09:00-18:00 local time, again every NUDGE_REPEAT seconds after "Not now".
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

# "You are working but the timer is off" popup (Shrey, 20 Nov 2026)
NUDGE_WINDOW = 60  # look at the last minute ...
NUDGE_MIN_ACTIVE = 15  # ... and ask when there was input in at least this many of its seconds
NUDGE_REPEAT = 5 * 60  # after "Not now": ask again 5 minutes later if still working with the timer off
WORK_DAYS = (0, 1, 2, 3, 4)  # Monday to Friday
WORK_FROM = 9 * 3600  # 09:00 local time
WORK_TO = 18 * 3600  # 18:00 local time


def in_work_hours(ts: float) -> bool:
	t = time.localtime(ts)
	secs = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
	return t.tm_wday in WORK_DAYS and WORK_FROM <= secs < WORK_TO


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
		self.tracking_enabled = True  # Timetrack Settings "enabled"
		self.nudge_enabled = True

		self.state = "stopped"  # stopped | running | paused
		self.session = None  # {"client_id", "started"}
		self.break_session = None
		self.samples: deque[Sample] = deque()
		self.blocks: dict[int, Block] = {}
		self.idle: Idle | None = None
		self.last_tick = None
		self._prev_app = None
		self._prev_url = None

		self.fg = Foreground(None)
		self._url = (None, None, 0.0)  # (app, domain, when)
		self._quiet_base = None  # idle answered while still away: count quiet time again from here
		self._active_secs: deque[int] = deque()  # seconds with input while the timer is off
		self._last_off_check = None
		self._nudge_after = 0  # no "timer is off" popup before this time
		self.stopped_at = None  # when the last automatic stop (away / asleep) cut the timer

		# display helpers
		self.server_today = 0
		self.server_today_date = None
		self.unsynced = 0  # tracked seconds in blocks queued but not yet confirmed by the server

		# callbacks (set by the UI)
		self.on_change = lambda: None
		self.on_idle_return = lambda idle: None
		self.on_idle_begin = lambda idle: None  # idle just started: show the question at once
		self.on_notice = lambda text: None
		self.on_nudge = lambda kind: None  # kind: stopped | paused | autostop | sleep

	# ------------------------------------------------------------------ settings
	def apply_settings(self, s: dict | None):
		if not s:
			return
		with self.lock:
			self.idle_timeout = max(60, int(s.get("idle_timeout_min") or 5) * 60)
			self.idle_autostop = max(self.idle_timeout + 60, int(s.get("idle_autostop_min") or 30) * 60)
			self.track_apps = bool(s.get("track_apps", 1))
			self.track_urls = bool(s.get("track_urls", 1))
			self.tracking_enabled = bool(s.get("enabled", 1))
			self.nudge_enabled = bool(s.get("nudge_enabled", 1))

	# ------------------------------------------------------------------ timer control
	def start(self, ts: float | None = None):
		"""Start the (general) work timer. Does nothing if it is already running."""
		with self.lock:
			now = int(ts or self.clock())
			if self.state == "running":
				return
			if self.state == "paused":
				self._end_break(now)
			self.session = {"client_id": new_client_id(), "started": now}
			self.outbox.put({"type": "start", "client_id": self.session["client_id"], "ts": now, "session_type": "Work"})
			self.state = "running"
			self.last_tick = now
			self._quiet_base = None
			self._active_secs.clear()
			self._last_off_check = None
			self._nudge_after = 0
			self._prev_app = self._prev_url = None
			self._persist()
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
			self._stop_work(now, "User")
			self.break_session = {"client_id": new_client_id(), "started": now}
			self.outbox.put({"type": "start", "client_id": self.break_session["client_id"], "ts": now, "session_type": "Break"})
			self.state = "paused"
			self.session = None
			self._persist()
		self.on_change()

	def resume(self):
		with self.lock:
			if self.state != "paused":
				return
		self.start()

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
				self._watch_off()
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
			if active:
				self._quiet_base = None
			elif self._quiet_base is not None:
				idle_s = min(idle_s, now - self._quiet_base)
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
			self.stopped_at = last
			self.state = "stopped"
			self.session = None
			self._persist()
			self.on_notice(f"Timer stopped at {time.strftime('%H:%M', time.localtime(last))} because the computer was asleep.")
			self._nudge_now("sleep", now)
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
		if self.idle.reason == "idle":
			self.on_idle_begin(self.idle)

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
		self.stopped_at = start
		self.state = "stopped"
		self.session = None
		self._persist()
		self.on_notice(f"Timer stopped at {time.strftime('%H:%M', time.localtime(start))} because you were away.")
		self._nudge_now("autostop", int(self.clock()))
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
				if not self.idle.returned:
					# answered while still away (a call, a meeting): the timer keeps running and the
					# next idle question comes after another full idle timeout without input
					self._quiet_base = int(self.clock())
				self._resolve_idle_locked(action)
		self.on_change()

	def _resolve_idle_locked(self, action: str):
		for s in self.samples:
			if s.kind == "idle":
				s.kind = "kept" if action == "keep" else "discarded"
		self.idle = None

	# ------------------------------------------------------------------ timer off while working
	def _watch_off(self):
		"""Called every second while the timer is stopped or on a break."""
		now = int(self.clock())
		if self._last_off_check is not None and now <= self._last_off_check:
			return
		self._last_off_check = now
		try:
			kb, mouse = self.platform.input_state()
			quiet = self.platform.idle_seconds()
		except Exception:
			return
		if kb or mouse or quiet < 1.5:
			self._active_secs.append(now)
		while self._active_secs and self._active_secs[0] <= now - NUDGE_WINDOW:
			self._active_secs.popleft()
		if len(self._active_secs) < NUDGE_MIN_ACTIVE or now < self._nudge_after:
			return
		self._nudge_now(self.state, now)

	def _nudge_now(self, kind: str, now: int):
		if not (self.nudge_enabled and self.tracking_enabled) or not in_work_hours(now):
			return
		self._nudge_after = now + NUDGE_REPEAT
		self.on_nudge(kind)

	def snooze_nudge(self):
		"""'Not now': ask again after NUDGE_REPEAT if the person is still working with the timer off."""
		with self.lock:
			now = int(self.clock())
			self._nudge_after = now + NUDGE_REPEAT
			self._active_secs.clear()

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
