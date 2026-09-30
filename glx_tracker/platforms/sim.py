"""Simulated computer for tests and for running on Linux: scripted apps, URLs, input and idle."""

import random
import time

from glx_tracker.platforms.base import Foreground, Platform

DEFAULT_APPS = [
	("Google Chrome", "https://sellercentral.amazon.ae/orders"),
	("Google Chrome", "https://www.youtube.com/watch?v=x"),
	("Microsoft Excel", None),
	("Telegram", None),
	("Google Chrome", "https://erp.globalex.me/desk/task"),
]


class SimPlatform(Platform):
	"""``script`` is a list of segments: (seconds, {"app", "url", "kb", "mouse", "idle"}).
	Without a script it produces random but plausible activity."""

	name = "sim"

	def __init__(self, clock=time.time, script=None, seed=1):
		self.clock = clock
		self.script = list(script or [])
		self.rng = random.Random(seed)
		self.start_at = None
		self.last_input = clock()
		self._seg_cache = None

	def _segment(self):
		now = self.clock()
		if self.start_at is None:
			self.start_at = now
		if not self.script:
			idx = int((now - self.start_at) // 120) % len(DEFAULT_APPS)
			app, url = DEFAULT_APPS[idx]
			r = self.rng.random()
			return {"app": app, "url": url, "kb": r < 0.3, "mouse": r < 0.6, "idle": False}
		t = now - self.start_at
		for dur, seg in self.script:
			if t < dur:
				return seg
			t -= dur
		return self.script[-1][1]

	def input_state(self):
		seg = self._segment()
		if seg.get("idle"):
			return False, False
		kb = bool(seg.get("kb")) if not isinstance(seg.get("kb"), float) else self.rng.random() < seg["kb"]
		mouse = bool(seg.get("mouse")) if not isinstance(seg.get("mouse"), float) else self.rng.random() < seg["mouse"]
		if kb or mouse:
			self.last_input = self.clock()
		return kb, mouse

	def idle_seconds(self):
		seg = self._segment()
		if not seg.get("idle"):
			# active segments may still have quiet seconds; that's not "idle" in the Hubstaff sense
			return max(0.0, min(self.clock() - self.last_input, 30.0))
		return self.clock() - self.last_input

	def foreground(self):
		seg = self._segment()
		return Foreground(seg.get("app"), pid=1)

	def browser_url(self, fg):
		seg = self._segment()
		return seg.get("url") if fg.browser else None
