"""Command-line mode, used for testing against a server without a GUI.

  python -m glx_tracker.headless --server http://site --email a@b --password x --replay 45

``--replay N`` simulates N minutes of work that end now (fast, with a fake clock).
"""

import argparse
import logging
import os
import sys
import tempfile
import time

from glx_tracker.api import Api
from glx_tracker.config import Config
from glx_tracker.outbox import Outbox
from glx_tracker.platforms.base import get_platform
from glx_tracker.platforms.sim import SimPlatform
from glx_tracker.syncer import Syncer
from glx_tracker.tracker import Tracker


class FakeClock:
	def __init__(self, t):
		self.t = t

	def __call__(self):
		return self.t


def main(argv=None):
	p = argparse.ArgumentParser()
	p.add_argument("--server", required=True)
	p.add_argument("--email", required=True)
	p.add_argument("--password", required=True)
	p.add_argument("--replay", type=int, default=0, help="simulate N minutes ending now")
	p.add_argument("--idle-at", type=int, default=0, help="minute at which to go idle (replay)")
	p.add_argument("--idle-for", type=int, default=0, help="idle minutes (replay)")
	p.add_argument("--idle-answer", default="discard", choices=["keep", "discard", "stop"])
	p.add_argument("--home", default=None)
	args = p.parse_args(argv)
	logging.basicConfig(level=logging.INFO)

	os.environ.setdefault("GLX_TRACKER_HOME", args.home or tempfile.mkdtemp(prefix="glx-tracker-"))
	os.environ.setdefault("GLX_TRACKER_NO_KEYRING", "1")
	cfg = Config()
	cfg.set("server", args.server)
	api = Api(args.server)
	plat = get_platform(simulate=True)
	info = api.register(args.email, args.password, cfg.get("device_id"), plat.hostname() + "-headless", *plat.os_info())
	cfg.set_token(info["device"], info["token"])
	api.device, api.token = info["device"], info["token"]
	print("signed in as", info["full_name"], "device", info["device"])


	from glx_tracker.config import data_dir

	outbox = Outbox(data_dir() / "outbox.db")
	if args.replay:
		start = int(time.time()) - args.replay * 60
		clock = FakeClock(start)
		active = {"app": "Google Chrome", "url": "https://sellercentral.amazon.ae/o", "kb": 0.3, "mouse": 0.7}
		script = []
		if args.idle_at and args.idle_for:
			script = [
				(args.idle_at * 60, active),
				(args.idle_for * 60, {"app": "Google Chrome", "url": "https://sellercentral.amazon.ae/o", "idle": True}),
				(10**9, {"app": "Microsoft Excel", "kb": 0.4, "mouse": 0.5}),
			]
		plat = SimPlatform(clock=clock, script=script)
		tr = Tracker(plat, outbox, cfg, clock=clock)
		tr.apply_settings(info.get("settings"))
		tr.on_notice = lambda m: print("notice:", m)
		tr.start()
		end = start + args.replay * 60
		while clock.t < end:
			clock.t += 1
			if tr.fg.browser:
				tr.set_url(tr.fg.app, plat.browser_url(tr.fg))
			tr.tick()
			if tr.idle and tr.idle.returned:
				tr.resolve_idle(args.idle_answer)
			if tr.state == "stopped":
				break
		if tr.state != "stopped":
			tr.stop()
		sync = Syncer(api, tr, outbox, cfg)
		sync.heartbeat_once()
		sync.sync_once()
		print("queued after sync:", outbox.count(), "today:", tr.server_today, "s")
		return 0
	print("nothing to do (use --replay)")
	return 0


if __name__ == "__main__":
	sys.exit(main())
