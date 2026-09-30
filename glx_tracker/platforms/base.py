"""What the tracker needs from the operating system."""

import platform
import socket
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

BROWSERS = {
	# app name (as reported by the OS, lower-case) -> key
	"google chrome": "chrome",
	"chrome": "chrome",
	"microsoft edge": "edge",
	"msedge": "edge",
	"brave browser": "brave",
	"brave": "brave",
	"arc": "arc",
	"vivaldi": "vivaldi",
	"opera": "opera",
	"safari": "safari",
	"firefox": "firefox",
	"mozilla firefox": "firefox",
	"yandex": "yandex",
	"yandex browser": "yandex",
}


@dataclass
class Foreground:
	app: str | None  # friendly app name, e.g. "Google Chrome", "Microsoft Excel"
	pid: int | None = None
	handle: object = None  # hwnd on Windows
	bundle: str | None = None  # bundle id on macOS

	@property
	def browser(self) -> str | None:
		return BROWSERS.get((self.app or "").lower())


def domain_from_url(url: str | None) -> str | None:
	if not url:
		return None
	url = url.strip()
	if not url or url.startswith(("about:", "chrome:", "edge:", "brave:", "file:", "view-source:", "chrome-extension:", "moz-extension:")):
		return None
	if "://" not in url:
		if " " in url or "." not in url.split("/")[0]:
			return None  # a search term typed in the address bar
		url = "http://" + url
	try:
		host = urlsplit(url).hostname
	except ValueError:
		return None
	if not host:
		return None
	host = host.lower()
	if host.startswith("www."):
		host = host[4:]
	return host


class Platform:
	name = "base"

	def start(self):
		"""Start background listeners (if any)."""

	def stop(self):
		pass

	def idle_seconds(self) -> float:
		"""Seconds since the last keyboard or mouse input."""
		raise NotImplementedError

	def input_state(self) -> tuple[bool, bool]:
		"""(keyboard used in the last second, mouse used in the last second)."""
		raise NotImplementedError

	def foreground(self) -> Foreground:
		raise NotImplementedError

	def browser_url(self, fg: Foreground) -> str | None:
		"""Current URL of the foreground browser window. May be slow - called off the UI thread."""
		return None

	def set_autostart(self, enabled: bool, command: list[str]):
		pass

	def missing_permissions(self) -> list[str]:
		return []

	def os_info(self) -> tuple[str, str]:
		if sys.platform == "darwin":
			return "macOS", platform.mac_ver()[0]
		if sys.platform.startswith("win"):
			return "Windows", platform.version()
		return platform.system(), platform.release()

	def hostname(self) -> str:
		return socket.gethostname().split(".")[0]


def get_platform(simulate: bool = False) -> Platform:
	if simulate:
		from glx_tracker.platforms.sim import SimPlatform

		return SimPlatform()
	if sys.platform == "darwin":
		from glx_tracker.platforms.mac import MacPlatform

		return MacPlatform()
	if sys.platform.startswith("win"):
		from glx_tracker.platforms.windows import WindowsPlatform

		return WindowsPlatform()
	from glx_tracker.platforms.sim import SimPlatform

	return SimPlatform()
