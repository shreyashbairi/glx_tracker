"""Background thread: sends the outbox to ERPNext, checks in, fetches commands and settings."""

import logging
import queue
import threading
import time

from glx_tracker import __version__
from glx_tracker.api import ApiError, AuthError

log = logging.getLogger("glx_tracker.sync")

MAX_ATTEMPTS = 40


class Syncer(threading.Thread):
	def __init__(self, api, tracker, outbox, config):
		super().__init__(daemon=True, name="glx-sync")
		self.api = api
		self.tracker = tracker
		self.outbox = outbox
		self.config = config
		self.commands: queue.Queue = queue.Queue()
		self.last_command = int(config.get("last_command") or 0)
		self.heartbeat_sec = 15
		self.online = False
		self.last_error = None
		self.last_ok = None
		self.settings = {}
		self.auth_lost = False
		self._stop = threading.Event()
		self._wake = threading.Event()
		self._next_heartbeat = 0.0
		self.on_status = lambda: None  # UI refresh hook
		self.on_auth_lost = lambda: None

	def stop(self):
		self._stop.set()
		self._wake.set()

	def wake(self):
		self._wake.set()

	def command_done(self, cmd_id: int):
		self.last_command = max(self.last_command, int(cmd_id))
		self.config.set("last_command", self.last_command)
		self._next_heartbeat = 0  # report quickly
		self.wake()

	# ------------------------------------------------------------------
	def run(self):
		while not self._stop.is_set():
			if not self.auth_lost:
				try:
					if time.time() >= self._next_heartbeat:
						self.heartbeat_once()
					if self.outbox.count():
						self.sync_once()
					self._set_online(True, None)
				except AuthError as e:
					log.warning("signed out by server: %s", e)
					self.auth_lost = True
					self._set_online(False, str(e))
					self.on_auth_lost()
				except ApiError as e:
					self._set_online(False, str(e))
				except Exception as e:  # never let the thread die
					log.exception("sync loop error")
					self._set_online(False, str(e))
			self._wake.wait(5 if self.online else 20)
			self._wake.clear()

	def _set_online(self, online: bool, error: str | None):
		changed = online != self.online or error != self.last_error
		self.online = online
		self.last_error = error
		if online:
			self.last_ok = time.time()
		if changed:
			self.on_status()

	def heartbeat_once(self):
		state = self.tracker.status()
		state.update({"agent_version": __version__, "last_command": self.last_command, "queued": self.outbox.count()})
		resp = self.api.heartbeat(state) or {}
		self._next_heartbeat = time.time() + self.heartbeat_sec
		if resp.get("settings"):
			self.settings = resp["settings"]
			self.heartbeat_sec = int(self.settings.get("heartbeat_sec") or 15)
			self.tracker.apply_settings(self.settings)
		self._apply_today(resp.get("today"))
		cmd = resp.get("command")
		if cmd and int(cmd.get("id") or 0) > self.last_command:
			self.commands.put(cmd)
		self.on_status()

	def sync_once(self):
		for _ in range(20):  # drain in batches
			events = self.outbox.peek(200)
			if not events:
				return
			resp = self.api.sync(events) or {}
			acked = set(resp.get("acked") or [])
			rejected = resp.get("rejected") or {}
			failed = resp.get("failed") or {}
			by_id = {e["id"]: e for e in events}

			acked_tracked = sum(int(by_id[i].get("tracked") or 0) for i in acked if i in by_id and by_id[i].get("type") == "block")
			self.outbox.remove(acked)
			with self.tracker.lock:
				self.tracker.unsynced = max(0, self.tracker.unsynced - acked_tracked)
			self._apply_today(resp.get("today"))

			for rid, message in rejected.items():
				ev = by_id.get(int(rid))
				if ev and ev.get("type") == "start":
					log.warning("server refused timer %s: %s", ev.get("client_id"), message)
					self.outbox.remove_client(ev["client_id"])
					self.tracker.abort(ev["client_id"], message)
				else:
					log.warning("server dropped event %s: %s", rid, message)

			if failed:
				rid = int(next(iter(failed)))
				attempts = self.outbox.bump_attempts(rid)
				log.warning("event %s failed (%s attempts): %s", rid, attempts, failed[str(rid)])
				if attempts >= MAX_ATTEMPTS:
					log.error("dropping event %s after %s attempts: %s", rid, attempts, by_id.get(rid))
					self.outbox.remove([rid])
				return
			if len(events) < 200:
				return

	def _apply_today(self, today):
		if not today:
			return
		with self.tracker.lock:
			self.tracker.server_today = int(today.get("tracked_sec") or 0)
			self.tracker.server_today_date = today.get("date")
