"""A fake CRM so the write path can be exercised locally.

It behaves like a CRM endpoint that accepts an idempotency key: sending the same
key twice returns the first write instead of making a second one. Many real CRM
APIs do not offer this. If yours doesn't, read the current owner before writing
and reconcile ambiguous outcomes (timeouts, lost responses) before retrying.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class CRMUnavailable(RuntimeError):
    pass


class FakeCRM:
    def __init__(self, path: Path):
        self.path = path
        self.fail_next: int = 0           # simulate outages: fail before writing
        self.lose_next_response: int = 0  # simulate timeouts: write, then fail
        with self._con() as con:
            con.execute(
                """create table if not exists crm_owner_writes (
                       idempotency_key text primary key,
                       action_id text not null,
                       lead_id text not null,
                       owner_or_queue text not null,
                       written_at text not null)"""
            )

    def _con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def assign_owner(self, lead_id: str, owner_or_queue: str, idempotency_key: str) -> str:
        if self.fail_next:
            self.fail_next -= 1
            raise CRMUnavailable("CRM returned 503")
        with self._con() as con:
            row = con.execute("select action_id from crm_owner_writes where idempotency_key = ?",
                              (idempotency_key,)).fetchone()
            if row:
                action_id = row[0]
            else:
                action_id = f"crm_{uuid.uuid4().hex[:10]}"
                con.execute("insert into crm_owner_writes values (?, ?, ?, ?, ?)",
                            (idempotency_key, action_id, lead_id, owner_or_queue,
                             datetime.now(timezone.utc).isoformat()))
        if self.lose_next_response:
            self.lose_next_response -= 1
            raise TimeoutError("CRM write may have succeeded, but the response was lost")
        return action_id

    def writes(self) -> list[tuple]:
        with self._con() as con:
            return con.execute("select lead_id, owner_or_queue, action_id from crm_owner_writes "
                               "order by written_at").fetchall()
