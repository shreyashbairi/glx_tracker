"""HTTP client for the glx_timetrack ERPNext app."""

import json
import logging

import requests

from glx_tracker import __version__

log = logging.getLogger("glx_tracker.api")


class ApiError(Exception):
	def __init__(self, message, status=None):
		super().__init__(message)
		self.status = status


class AuthError(ApiError):
	"""Token rejected (device removed, user disabled) -> sign in again."""


class Api:
	def __init__(self, server: str, device: str | None = None, token: str | None = None, timeout: int = 20):
		self.server = server.rstrip("/")
		self.device = device
		self.token = token
		self.timeout = timeout
		self.session = requests.Session()
		self.session.headers["User-Agent"] = f"GLX-Tracker/{__version__}"

	def _call(self, method: str, data: dict | None = None, auth: bool = True):
		headers = {}
		if auth:
			headers["X-GLX-Device"] = f"{self.device}:{self.token}"
		url = f"{self.server}/api/method/{method}"
		try:
			r = self.session.post(url, data=data or {}, headers=headers, timeout=self.timeout)
		except requests.RequestException as e:
			raise ApiError(f"Cannot reach {self.server}: {e.__class__.__name__}") from e
		try:
			body = r.json()
		except ValueError:
			body = {}
		if r.status_code == 401 and auth:
			raise AuthError(_server_message(body) or "Signed out", r.status_code)
		if r.status_code >= 400:
			raise ApiError(_server_message(body) or f"Server error {r.status_code}", r.status_code)
		return body.get("message")

	# ------------------------------------------------------------------
	def register(self, email, password, device_id, hostname, os_name, os_version):
		msg = self._call(
			"glx_timetrack.api.agent.register",
			{
				"usr": email,
				"pwd": password,
				"device": device_id or "",
				"hostname": hostname,
				"os_name": os_name,
				"os_version": os_version,
				"agent_version": __version__,
			},
			auth=False,
		)
		if not msg or not msg.get("ok"):
			raise ApiError((msg or {}).get("message") or "Sign-in failed")
		return msg

	def me(self):
		return self._call("glx_timetrack.api.agent.me")

	def heartbeat(self, state: dict):
		return self._call("glx_timetrack.api.agent.heartbeat", {"state": json.dumps(state)})

	def sync(self, events: list[dict]):
		return self._call("glx_timetrack.api.agent.sync", {"events": json.dumps(events)})

	def signout(self):
		return self._call("glx_timetrack.api.agent.signout")


def _server_message(body: dict) -> str | None:
	if not isinstance(body, dict):
		return None
	msg = body.get("message")
	if isinstance(msg, dict) and msg.get("message"):
		return msg["message"]
	if isinstance(msg, str):
		return msg
	server_messages = body.get("_server_messages")
	if server_messages:
		try:
			first = json.loads(json.loads(server_messages)[0])
			return first.get("message")
		except Exception:
			pass
	exc = body.get("exception")
	if isinstance(exc, str):
		return exc.split(":", 1)[-1].strip()
	return None
