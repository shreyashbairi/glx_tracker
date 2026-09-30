"""Tracker state machine tests with a fake clock and a scripted computer (no network, no GUI)."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from glx_tracker.outbox import Outbox
from glx_tracker.platforms.sim import SimPlatform
from glx_tracker.tracker import Tracker

T0 = 1_790_000_400  # aligned to a 10-minute slot
ACTIVE = {"app": "Google Chrome", "url": "https://sellercentral.amazon.ae/x", "kb": True, "mouse": True}
EXCEL = {"app": "Microsoft Excel", "kb": False, "mouse": True}
IDLE = {"app": "Google Chrome", "url": "https://sellercentral.amazon.ae/x", "idle": True}


class Clock:
	def __init__(self, t):
		self.t = t

	def __call__(self):
		return self.t


def make(script, start_offset=30):
	clock = Clock(T0 + start_offset)
	plat = SimPlatform(clock=clock, script=script)
	box = Outbox(os.path.join(tempfile.mkdtemp(), "o.db"))
	tr = Tracker(plat, box, clock=clock)
	tr.apply_settings({"idle_timeout_min": 5, "idle_autostop_min": 30, "track_apps": 1, "track_urls": 1})
	return clock, plat, box, tr


def run(clock, tr, seconds, plat=None, on_idle=None):
	for _ in range(seconds):
		clock.t += 1
		if plat is not None and tr.fg.browser:
			tr.set_url(tr.fg.app, plat.browser_url(tr.fg))
		tr.tick()
		if on_idle and tr.idle and tr.idle.returned:
			on_idle(tr)


def events(box):
	return box.peek(10000)


def blocks(box):
	return [e for e in events(box) if e["type"] == "block"]


def test_plain_work_blocks_and_stop():
	clock, plat, box, tr = make([(10_000, ACTIVE)])
	tr.start()
	run(clock, tr, 1500, plat)
	tr.stop()
	evs = events(box)
	assert evs[0]["type"] == "start" and evs[-1]["type"] == "stop"
	bl = blocks(box)
	assert [b["slot"] for b in bl] == [T0, T0 + 600, T0 + 1200]
	assert sum(b["tracked"] for b in bl) == 1500
	assert bl[0]["tracked"] == 570 and bl[1]["tracked"] == 600 and bl[2]["tracked"] == 330
	assert all(b["keyboard"] == b["tracked"] for b in bl)
	urls = [t for t in bl[1]["tools"] if t["t"] == "URL"]
	assert urls and urls[0]["n"] == "sellercentral.amazon.ae" and urls[0]["app"] == "Google Chrome"
	assert tr.state == "stopped"


def test_blocks_are_sent_while_running():
	clock, plat, box, tr = make([(10_000, EXCEL)])
	tr.start()
	run(clock, tr, 1800)
	# samples younger than idle timeout + 10 s are held back; older complete blocks are queued
	bl = blocks(box)
	assert [b["slot"] for b in bl] == [T0, T0 + 600]
	assert bl[0]["keyboard"] == 0 and bl[0]["mouse"] == bl[0]["tracked"]


def test_idle_keep():
	clock, plat, box, tr = make([(600, ACTIVE), (480, IDLE), (10_000, ACTIVE)])
	seen = []
	tr.on_idle_return = lambda idle: seen.append(idle.start)
	tr.start()
	run(clock, tr, 1500, on_idle=lambda t: t.resolve_idle("keep"))
	tr.stop()
	bl = blocks(box)
	assert seen, "idle dialog should have been triggered"
	assert sum(b["tracked"] for b in bl) == 1500
	assert sum(b["idle_kept"] for b in bl) >= 480 - 2
	assert sum(b["idle_discarded"] for b in bl) == 0


def test_idle_discard():
	clock, plat, box, tr = make([(600, ACTIVE), (480, IDLE), (10_000, ACTIVE)])
	tr.start()
	run(clock, tr, 1500, on_idle=lambda t: t.resolve_idle("discard"))
	tr.stop()
	bl = blocks(box)
	discarded = sum(b["idle_discarded"] for b in bl)
	assert 478 <= discarded <= 481
	assert sum(b["tracked"] for b in bl) == 1500 - discarded
	assert tr.state == "stopped"


def test_idle_discard_and_stop():
	clock, plat, box, tr = make([(600, ACTIVE), (480, IDLE), (10_000, ACTIVE)])
	tr.start()
	run(clock, tr, 1100, on_idle=lambda t: t.resolve_idle("stop"))
	assert tr.state == "stopped"
	stop = [e for e in events(box) if e["type"] == "stop"][0]
	assert stop["reason"] == "Idle"
	assert abs(stop["ts"] - (T0 + 30 + 600)) <= 2
	assert sum(b["tracked"] for b in blocks(box)) in range(598, 602)


def test_idle_autostop():
	clock, plat, box, tr = make([(600, ACTIVE), (100_000, IDLE)])
	notes = []
	tr.on_notice = notes.append
	tr.start()
	run(clock, tr, 600 + 31 * 60)
	assert tr.state == "stopped"
	stop = [e for e in events(box) if e["type"] == "stop"][0]
	assert abs(stop["ts"] - (T0 + 30 + 600)) <= 2 and stop["reason"] == "Idle"
	assert notes and "away" in notes[0]
	assert sum(b["tracked"] for b in blocks(box)) in range(598, 602)


def test_sleep_gap_stops_timer():
	clock, plat, box, tr = make([(100_000, ACTIVE)])
	tr.start()
	run(clock, tr, 300)
	clock.t += 2 * 3600  # laptop lid closed for two hours
	tr.tick()
	assert tr.state == "stopped"
	stop = [e for e in events(box) if e["type"] == "stop"][0]
	assert stop["reason"] == "Sleep" and stop["ts"] == T0 + 30 + 300
	assert sum(b["tracked"] for b in blocks(box)) == 300


def test_short_sleep_gap_becomes_idle_question():
	clock, plat, box, tr = make([(100_000, ACTIVE)])
	asked = []
	tr.on_idle_return = lambda idle: asked.append(idle.reason)
	tr.start()
	run(clock, tr, 300)
	clock.t += 600  # 10 minutes asleep
	tr.tick()
	assert asked == ["sleep"]
	tr.resolve_idle("discard")
	run(clock, tr, 60)
	tr.stop()
	bl = blocks(box)
	assert sum(b["tracked"] for b in bl) == 361
	assert sum(b["idle_discarded"] for b in bl) == 599


def test_start_pause_resume():
	clock, plat, box, tr = make([(100_000, EXCEL)])
	tr.start()
	run(clock, tr, 100)
	tr.start()  # already running: no-op
	run(clock, tr, 100)
	tr.pause()
	run(clock, tr, 50)
	assert tr.state == "paused"
	tr.resume()
	run(clock, tr, 100)
	tr.stop()
	kinds = [(e["type"], e.get("session_type"), e.get("reason")) for e in events(box) if e["type"] != "block"]
	assert kinds == [
		("start", "Work", None),
		("stop", None, "User"),
		("start", "Break", None),
		("stop", None, "User"),
		("start", "Work", None),
		("stop", None, "User"),
	]
	assert not any("task" in e for e in events(box))
	assert sum(b["tracked"] for b in blocks(box)) == 300


def test_abort_on_server_refusal():
	clock, plat, box, tr = make([(100_000, EXCEL)])
	tr.start()
	cid = tr.session["client_id"]
	run(clock, tr, 50)
	tr.abort(cid, "Time tracking is switched off")
	assert tr.state == "stopped" and not tr.samples


if __name__ == "__main__":
	import inspect

	failures = 0
	for name, fn in list(globals().items()):
		if name.startswith("test_") and inspect.isfunction(fn):
			try:
				fn()
				print("ok  ", name)
			except AssertionError as e:
				failures += 1
				print("FAIL", name, e)
				import traceback

				traceback.print_exc()
	sys.exit(1 if failures else 0)
