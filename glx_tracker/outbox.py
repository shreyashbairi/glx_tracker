"""Persistent, ordered queue of events waiting to be sent to ERPNext (survives crashes/offline)."""

import json
import sqlite3
import threading
from pathlib import Path


class Outbox:
	def __init__(self, path: Path):
		self.lock = threading.Lock()
		self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
		self.db.execute("pragma journal_mode=wal")
		self.db.execute(
			"create table if not exists outbox (id integer primary key autoincrement, "
			"client_id text, kind text, payload text not null, attempts integer default 0)"
		)

	def put(self, event: dict):
		with self.lock:
			self.db.execute(
				"insert into outbox (client_id, kind, payload) values (?, ?, ?)",
				(event.get("client_id"), event.get("type"), json.dumps(event)),
			)

	def peek(self, limit: int = 200) -> list[dict]:
		with self.lock:
			rows = self.db.execute("select id, payload from outbox order by id limit ?", (limit,)).fetchall()
		out = []
		for rid, payload in rows:
			ev = json.loads(payload)
			ev["id"] = rid
			out.append(ev)
		return out

	def remove(self, ids):
		ids = [int(i) for i in ids]
		if not ids:
			return
		with self.lock:
			self.db.executemany("delete from outbox where id = ?", [(i,) for i in ids])

	def remove_client(self, client_id: str):
		with self.lock:
			self.db.execute("delete from outbox where client_id = ?", (client_id,))

	def bump_attempts(self, rid: int) -> int:
		with self.lock:
			self.db.execute("update outbox set attempts = attempts + 1 where id = ?", (rid,))
			row = self.db.execute("select attempts from outbox where id = ?", (rid,)).fetchone()
		return row[0] if row else 0

	def count(self) -> int:
		with self.lock:
			return self.db.execute("select count(*) from outbox").fetchone()[0]

	def clear(self):
		with self.lock:
			self.db.execute("delete from outbox")
