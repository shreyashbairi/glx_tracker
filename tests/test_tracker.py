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



# ---------------------------------------------------------------- v0.3.0: idle question at once, "timer is off" popup
import time as _time

MON_10 = int(_time.mktime((2026, 11, 23, 10, 0, 0, 0, 0, -1)))  # a Monday, 10:00 local time
MON_17_59 = int(_time.mktime((2026, 11, 23, 17, 59, 0, 0, 0, -1)))
MON_20 = int(_time.mktime((2026, 11, 23, 20, 0, 0, 0, 0, -1)))
SAT_10 = int(_time.mktime((2026, 11, 28, 10, 0, 0, 0, 0, -1)))


def make_at(script, t, settings=None):
	clock = Clock(t)
	plat = SimPlatform(clock=clock, script=script)
	box = Outbox(os.path.join(tempfile.mkdtemp(), "o.db"))
	tr = Tracker(plat, box, clock=clock)
	tr.apply_settings(dict({"idle_timeout_min": 10, "idle_autostop_min": 15, "track_apps": 1, "track_urls": 1}, **(settings or {})))
	return clock, plat, box, tr


def test_idle_question_shown_when_idle_begins():
	clock, plat, box, tr = make([(600, ACTIVE), (100_000, IDLE)])
	begun = []
	tr.on_idle_begin = lambda idle: begun.append((int(clock()), idle.start))
	tr.start()
	run(clock, tr, 600 + 5 * 60 + 5)
	assert len(begun) == 1, begun
	shown_at, start = begun[0]
	assert abs(start - (T0 + 30 + 600)) <= 2  # idle counted from the last input
	assert abs(shown_at - (start + 5 * 60)) <= 2  # shown at the idle timeout, while still away
	assert tr.state == "running"


def test_keep_while_away_keeps_timer_running():
	# 10 min idle timeout, 15 min auto-stop (production settings). Person in a meeting answers Keep.
	clock, plat, box, tr = make_at([(600, ACTIVE), (100_000, IDLE)], MON_10)
	begun = []

	def on_begin(idle):
		begun.append(int(clock()))
		if len(begun) == 1:
			tr.resolve_idle("keep")  # "Keep idle time" clicked while away

	tr.on_idle_begin = on_begin
	tr.start()
	run(clock, tr, 600 + 10 * 60 + 5)
	assert len(begun) == 1 and tr.state == "running" and tr.idle is None
	run(clock, tr, 9 * 60)  # 19 min without input: without the answer it would have stopped at 15
	assert tr.state == "running" and len(begun) == 1
	run(clock, tr, 2 * 60)  # 10 min after the answer: asked again
	assert len(begun) == 2 and abs(begun[1] - begun[0] - 600) <= 3
	run(clock, tr, 6 * 60)  # second question not answered: stops 15 min after the answer
	assert tr.state == "stopped"
	stop = [e for e in events(box) if e["type"] == "stop"][0]
	assert stop["reason"] == "Idle" and abs(stop["ts"] - begun[0]) <= 2
	bl = blocks(box)
	assert sum(b["idle_kept"] for b in bl) >= 600 - 3  # the kept first 10 minutes count


def test_nudge_when_working_with_timer_off():
	clock, plat, box, tr = make_at([(100_000, EXCEL)], MON_10)
	kinds = []
	tr.on_nudge = lambda kind: kinds.append((kind, int(clock())))
	run(clock, tr, 14)
	assert not kinds  # a few seconds of mouse movement is not "working" yet
	run(clock, tr, 2)
	assert len(kinds) == 1 and kinds[0][0] == "stopped" and kinds[0][1] - MON_10 in range(14, 17)
	tr.snooze_nudge()  # "Not now"
	run(clock, tr, 299)
	assert len(kinds) == 1
	run(clock, tr, 2)
	assert len(kinds) == 2  # asked again 5 minutes later, still working
	tr.start()
	run(clock, tr, 600)
	assert len(kinds) == 2  # timer running: no popup


def test_nudge_when_on_a_break():
	clock, plat, box, tr = make_at([(100_000, EXCEL)], MON_10)
	kinds = []
	tr.on_nudge = kinds.append
	tr.start()
	run(clock, tr, 60)
	tr.pause()
	run(clock, tr, 20)
	assert kinds == ["paused"]
	tr.resume()
	assert tr.state == "running"


def test_no_nudge_when_away_or_outside_hours():
	for t in (MON_20, SAT_10):
		clock, plat, box, tr = make_at([(100_000, EXCEL)], t)
		kinds = []
		tr.on_nudge = kinds.append
		run(clock, tr, 900)
		assert not kinds, (t, kinds)
	clock, plat, box, tr = make_at([(100_000, IDLE)], MON_10)
	kinds = []
	tr.on_nudge = kinds.append
	run(clock, tr, 900)
	assert not kinds  # computer not in use
	clock, plat, box, tr = make_at([(100_000, EXCEL)], MON_17_59)
	kinds = []
	tr.on_nudge = kinds.append
	run(clock, tr, 300)
	assert len(kinds) == 1  # 17:59:16 yes, nothing after 18:00
	clock, plat, box, tr = make_at([(100_000, EXCEL)], MON_10, {"nudge_enabled": 0})
	kinds = []
	tr.on_nudge = kinds.append
	run(clock, tr, 300)
	assert not kinds


def test_autostop_shows_timer_off_popup():
	clock, plat, box, tr = make_at([(600, ACTIVE), (100_000, IDLE)], MON_10)
	kinds = []
	tr.on_nudge = kinds.append
	tr.start()
	run(clock, tr, 600 + 15 * 60 + 5)
	assert tr.state == "stopped" and kinds == ["autostop"]
	run(clock, tr, 600)
	assert kinds == ["autostop"]  # still away: nothing more


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
