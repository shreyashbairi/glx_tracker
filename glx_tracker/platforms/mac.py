"""macOS: Quartz for input/idle, NSWorkspace for the front app, AppleScript for browser URLs.

Permissions (System Settings > Privacy & Security):
- Input Monitoring: needed to tell keyboard from mouse (without it keyboard time shows as 0,
  overall activity still works).
- Automation: asked once per browser the first time we read its address bar.
No Screen Recording or Accessibility permission is needed: we never read window titles or content.
"""

import ctypes
import logging
import plistlib
import subprocess
from pathlib import Path

import Quartz
from AppKit import NSWorkspace

from glx_tracker.platforms.base import Foreground, Platform

log = logging.getLogger("glx_tracker.mac")

HID = Quartz.kCGEventSourceStateHIDSystemState
ANY = Quartz.kCGAnyInputEventType
KEY_EVENTS = (Quartz.kCGEventKeyDown, Quartz.kCGEventFlagsChanged)
MOUSE_EVENTS = (
	Quartz.kCGEventMouseMoved,
	Quartz.kCGEventLeftMouseDown,
	Quartz.kCGEventRightMouseDown,
	Quartz.kCGEventOtherMouseDown,
	Quartz.kCGEventLeftMouseDragged,
	Quartz.kCGEventScrollWheel,
)

CHROMIUM_BUNDLES = {
	"com.google.Chrome",
	"com.google.Chrome.beta",
	"com.microsoft.edgemac",
	"com.brave.Browser",
	"company.thebrowser.Browser",
	"com.vivaldi.Vivaldi",
	"com.operasoftware.Opera",
	"ru.yandex.desktop.yandex-browser",
}
SAFARI_BUNDLES = {"com.apple.Safari", "com.apple.SafariTechnologyPreview"}

# nicer / Hubstaff-compatible names for a few apps
RENAMES = {"zoom.us": "Zoom"}

_IOKIT = None


def _iokit():
	global _IOKIT
	if _IOKIT is None:
		try:
			_IOKIT = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
			_IOKIT.IOHIDCheckAccess.restype = ctypes.c_int
			_IOKIT.IOHIDCheckAccess.argtypes = [ctypes.c_int]
			_IOKIT.IOHIDRequestAccess.restype = ctypes.c_bool
			_IOKIT.IOHIDRequestAccess.argtypes = [ctypes.c_int]
		except Exception:
			_IOKIT = False
	return _IOKIT


class MacPlatform(Platform):
	name = "mac"

	def idle_seconds(self) -> float:
		return float(Quartz.CGEventSourceSecondsSinceLastEventType(HID, ANY))

	def input_state(self) -> tuple[bool, bool]:
		kb = min(Quartz.CGEventSourceSecondsSinceLastEventType(HID, t) for t in KEY_EVENTS) < 1.05
		mouse = min(Quartz.CGEventSourceSecondsSinceLastEventType(HID, t) for t in MOUSE_EVENTS) < 1.05
		return kb, mouse

	def foreground(self) -> Foreground:
		app = NSWorkspace.sharedWorkspace().frontmostApplication()
		if app is None:
			return Foreground(None)
		name = str(app.localizedName() or "")
		return Foreground(
			RENAMES.get(name, name) or None,
			pid=int(app.processIdentifier()),
			bundle=str(app.bundleIdentifier() or ""),
		)

	def browser_url(self, fg: Foreground) -> str | None:
		if fg.bundle in CHROMIUM_BUNDLES:
			script = f'tell application id "{fg.bundle}" to get URL of active tab of front window'
		elif fg.bundle in SAFARI_BUNDLES:
			script = f'tell application id "{fg.bundle}" to get URL of front document'
		else:
			return None  # Firefox has no AppleScript support for the URL
		try:
			out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
		except Exception:
			return None
		if out.returncode != 0:
			if "-1743" in out.stderr:  # not authorised to send Apple events
				log.info("Automation permission missing for %s", fg.bundle)
			return None
		return out.stdout.strip() or None

	# ------------------------------------------------------------------ permissions
	def missing_permissions(self) -> list[str]:
		io = _iokit()
		if io and io.IOHIDCheckAccess(1) != 0:  # kIOHIDRequestTypeListenEvent
			return ["Input Monitoring"]
		return []

	def request_permissions(self):
		io = _iokit()
		if io:
			io.IOHIDRequestAccess(1)

	def open_privacy_settings(self):
		subprocess.run(
			["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"], check=False
		)

	# ------------------------------------------------------------------ autostart
	def set_autostart(self, enabled: bool, command: list[str]):
		plist = Path.home() / "Library" / "LaunchAgents" / "me.globalex.tracker.plist"
		if not enabled:
			plist.unlink(missing_ok=True)
			return
		plist.parent.mkdir(parents=True, exist_ok=True)
		with open(plist, "wb") as f:
			plistlib.dump(
				{
					"Label": "me.globalex.tracker",
					"ProgramArguments": command,
					"RunAtLoad": True,
					"ProcessType": "Interactive",
				},
				f,
			)
