"""Where the tracker keeps its files, settings and sign-in token."""

import json
import logging
import logging.handlers
import os
import sys
import uuid
from pathlib import Path

from glx_tracker import APP_NAME, DEFAULT_SERVER

log = logging.getLogger("glx_tracker")


def data_dir() -> Path:
	override = os.environ.get("GLX_TRACKER_HOME")
	if override:
		p = Path(override)
	elif sys.platform == "darwin":
		p = Path.home() / "Library" / "Application Support" / APP_NAME
	elif sys.platform.startswith("win"):
		p = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
	else:
		p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "glx-tracker"
	p.mkdir(parents=True, exist_ok=True)
	return p


def setup_logging(debug: bool = False):
	handler = logging.handlers.RotatingFileHandler(data_dir() / "tracker.log", maxBytes=2_000_000, backupCount=3)
	handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
	root = logging.getLogger("glx_tracker")
	root.setLevel(logging.DEBUG if debug else logging.INFO)
	root.addHandler(handler)
	if debug:
		root.addHandler(logging.StreamHandler())


class Config:
	"""Small JSON file for non-secret settings; the token lives in the OS keychain."""

	KEYRING_SERVICE = APP_NAME

	def __init__(self):
		self.path = data_dir() / "config.json"
		self.data = {}
		if self.path.exists():
			try:
				self.data = json.loads(self.path.read_text())
			except Exception:
				log.exception("config unreadable, starting fresh")
		self.data.setdefault("server", DEFAULT_SERVER)
		self.data.setdefault("device_id", "")
		self.data.setdefault("autostart", True)
		self.data.setdefault("recent_tasks", [])

	def get(self, key, default=None):
		return self.data.get(key, default)

	def set(self, key, value):
		self.data[key] = value
		self.save()

	def save(self):
		tmp = self.path.with_suffix(".tmp")
		tmp.write_text(json.dumps(self.data, indent=2))
		os.replace(tmp, self.path)
		try:
			os.chmod(self.path, 0o600)
		except Exception:
			pass

	# ------------------------------------------------------------- token
	def _keyring(self):
		if os.environ.get("GLX_TRACKER_NO_KEYRING"):
			return None
		try:
			import keyring

			return keyring
		except Exception:
			return None

	def get_token(self) -> str | None:
		device = self.get("device_id")
		if not device:
			return None
		kr = self._keyring()
		if kr:
			try:
				tok = kr.get_password(self.KEYRING_SERVICE, device)
				if tok:
					return tok
			except Exception:
				log.warning("keyring read failed", exc_info=True)
		return self.get("token_fallback")

	def set_token(self, device: str, token: str | None):
		kr = self._keyring()
		stored = False
		if kr:
			try:
				if token:
					kr.set_password(self.KEYRING_SERVICE, device, token)
				else:
					try:
						kr.delete_password(self.KEYRING_SERVICE, device)
					except Exception:
						pass
				stored = True
			except Exception:
				log.warning("keyring write failed, using config file", exc_info=True)
		self.data["token_fallback"] = None if stored or not token else token
		self.data["device_id"] = device
		self.save()

	@property
	def signed_in(self) -> bool:
		return bool(self.get("device_id") and self.get_token())

	def remember_task(self, task: dict | None):
		if not task or not task.get("name"):
			return
		recent = [t for t in self.data.get("recent_tasks", []) if t.get("name") != task["name"]]
		recent.insert(0, {k: task.get(k) for k in ("name", "subject", "project_name")})
		self.set("recent_tasks", recent[:10])


def new_client_id() -> str:
	return str(uuid.uuid4())
