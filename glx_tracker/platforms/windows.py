"""Windows: Win32 via ctypes for idle/front window, pynput for keyboard vs mouse, UI Automation for URLs.

Nothing here needs admin rights. We never read keys, window titles or page content: only
"was there keyboard/mouse input this second", the front app's name and the browser's domain.
"""

import ctypes
import ctypes.wintypes as wt
import logging
import os
import threading
import time
import winreg

from glx_tracker.platforms.base import Foreground, Platform

log = logging.getLogger("glx_tracker.win")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
version = ctypes.WinDLL("version", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
	_fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


kernel32.GetTickCount64.restype = ctypes.c_ulonglong
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]

# executable -> nicer name when the file has no useful description
EXE_NAMES = {
	"chrome.exe": "Google Chrome",
	"msedge.exe": "Microsoft Edge",
	"firefox.exe": "Firefox",
	"brave.exe": "Brave Browser",
	"opera.exe": "Opera",
	"browser.exe": "Yandex",
	"excel.exe": "Microsoft Excel",
	"winword.exe": "Microsoft Word",
	"outlook.exe": "Microsoft Outlook",
	"olk.exe": "Microsoft Outlook",
	"powerpnt.exe": "Microsoft PowerPoint",
	"onenote.exe": "Microsoft OneNote",
	"telegram.exe": "Telegram",
	"whatsapp.exe": "WhatsApp",
	"whatsapp.root.exe": "WhatsApp",
	"explorer.exe": "File Explorer",
	"ms-teams.exe": "Microsoft Teams",
	"teams.exe": "Microsoft Teams",
	"zoom.exe": "Zoom",
	"acrobat.exe": "Adobe Acrobat",
	"code.exe": "Code",
}
IGNORED = {"lockapp.exe", "applicationframehost.exe"}


def _exe_path(pid: int) -> str | None:
	h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
	if not h:
		return None
	try:
		buf = ctypes.create_unicode_buffer(1024)
		size = wt.DWORD(1024)
		if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
			return buf.value
	finally:
		kernel32.CloseHandle(h)
	return None


def _file_description(path: str) -> str | None:
	try:
		size = version.GetFileVersionInfoSizeW(path, None)
		if not size:
			return None
		data = ctypes.create_string_buffer(size)
		if not version.GetFileVersionInfoW(path, 0, size, data):
			return None
		ptr = ctypes.c_void_p()
		length = wt.UINT()
		if not version.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)):
			return None
		lang, codepage = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ushort * 2)).contents
		key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
		if not version.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(length)) or not length.value:
			return None
		return ctypes.wstring_at(ptr, length.value - 1).strip() or None
	except Exception:
		return None


class WindowsPlatform(Platform):
	name = "windows"

	def __init__(self):
		self._last_kb = 0.0
		self._last_mouse = 0.0
		self._listeners = []
		self._names = {}
		self._uia_cache = {}  # hwnd -> address bar control
		self._uia_local = threading.local()

	# ------------------------------------------------------------------ input
	def start(self):
		try:
			from pynput import keyboard, mouse

			def kb(*_a):
				self._last_kb = time.monotonic()

			def ms(*_a):
				self._last_mouse = time.monotonic()

			k = keyboard.Listener(on_press=kb)
			m = mouse.Listener(on_move=ms, on_click=ms, on_scroll=ms)
			k.daemon = m.daemon = True
			k.start()
			m.start()
			self._listeners = [k, m]
		except Exception:
			log.exception("input listeners unavailable; keyboard/mouse split disabled")

	def stop(self):
		for listener in self._listeners:
			try:
				listener.stop()
			except Exception:
				pass

	def idle_seconds(self) -> float:
		info = LASTINPUTINFO()
		info.cbSize = ctypes.sizeof(LASTINPUTINFO)
		if not user32.GetLastInputInfo(ctypes.byref(info)):
			return 0.0
		now = kernel32.GetTickCount64() & 0xFFFFFFFF
		return max(0, (now - info.dwTime) & 0xFFFFFFFF) / 1000.0

	def input_state(self) -> tuple[bool, bool]:
		now = time.monotonic()
		kb = now - self._last_kb < 1.05
		mouse = now - self._last_mouse < 1.05
		if not self._listeners and self.idle_seconds() < 1.05:
			mouse = True  # no hooks: count any input as mouse so activity % still works
		return kb, mouse

	# ------------------------------------------------------------------ front app
	def foreground(self) -> Foreground:
		hwnd = user32.GetForegroundWindow()
		if not hwnd:
			return Foreground(None)
		pid = wt.DWORD()
		user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
		path = _exe_path(pid.value)
		exe = os.path.basename(path or "").lower()
		if exe == "applicationframehost.exe":
			child_pid = self._uwp_child_pid(hwnd, pid.value)
			if child_pid:
				path = _exe_path(child_pid)
				exe = os.path.basename(path or "").lower()
		if not path or exe in IGNORED:
			return Foreground(None, pid=pid.value, handle=hwnd)
		return Foreground(self._friendly(path), pid=pid.value, handle=hwnd)

	def _uwp_child_pid(self, hwnd, host_pid) -> int | None:
		found = []

		@ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
		def cb(child, _lparam):
			p = wt.DWORD()
			user32.GetWindowThreadProcessId(child, ctypes.byref(p))
			if p.value and p.value != host_pid:
				found.append(p.value)
				return False
			return True

		user32.EnumChildWindows(hwnd, cb, 0)
		return found[0] if found else None

	def _friendly(self, path: str) -> str:
		if path in self._names:
			return self._names[path]
		exe = os.path.basename(path).lower()
		name = EXE_NAMES.get(exe) or _file_description(path) or os.path.splitext(os.path.basename(path))[0]
		self._names[path] = name
		return name

	# ------------------------------------------------------------------ browser URL
	def browser_url(self, fg: Foreground) -> str | None:
		if not fg.handle:
			return None
		try:
			import uiautomation as auto
		except Exception:
			return None
		if not getattr(self._uia_local, "init", None):
			self._uia_local.init = auto.UIAutomationInitializerInThread(debug=False)
		key = int(fg.handle)
		ctrl = self._uia_cache.get(key)
		try:
			if ctrl is None or not ctrl.Exists(0, 0):
				ctrl = self._find_address_bar(auto, fg.handle)
				if ctrl is None:
					return None
				self._uia_cache[key] = ctrl
				if len(self._uia_cache) > 50:
					self._uia_cache.pop(next(iter(self._uia_cache)))
			return ctrl.GetValuePattern().Value
		except Exception:
			self._uia_cache.pop(key, None)
			return None

	@staticmethod
	def _find_address_bar(auto, hwnd):
		from glx_tracker.platforms.base import domain_from_url

		window = auto.ControlFromHandle(hwnd)
		for ctrl, _depth in auto.WalkControl(window, maxDepth=14):
			if ctrl.ControlTypeName != "EditControl":
				continue
			try:
				value = ctrl.GetValuePattern().Value
			except Exception:
				continue
			if not value or domain_from_url(value):
				return ctrl  # address bar (may be empty on a new tab)
		return None

	# ------------------------------------------------------------------ autostart
	def set_autostart(self, enabled: bool, command: list[str]):
		key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
		try:
			if enabled:
				winreg.SetValueEx(key, "GLX Tracker", 0, winreg.REG_SZ, " ".join(f'"{c}"' for c in command))
			else:
				try:
					winreg.DeleteValue(key, "GLX Tracker")
				except FileNotFoundError:
					pass
		finally:
			winreg.CloseKey(key)
