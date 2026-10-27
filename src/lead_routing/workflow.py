"""Workflow ledger: one case per (lead, input fingerprint, policy version).

The ledger is what makes the process safe to re-run. Routing the same lead with
the same inputs and policy returns the existing case instead of creating a new
one, and an approved case is written to the CRM at most once per idempotency key.

    previewed ──approve──▶ approved ──apply──▶ applying ──▶ applied
    review_required ──resolve (human picks owner)──▶ approved
    applying ──CRM error──▶ failed ──apply (retry)──▶ applying
    any open case ──new inputs or policy version──▶ superseded
    duplicate (terminal, nothing to write)

SQLite is enough to show the pattern on one machine. With several workers you
need a server database with row locks or a durable workflow engine, and a
database row alone still can't make an external write exactly-once: that needs
an idempotent CRM call or a reconcile step.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import input_fingerprint
from .crm_stub import FakeCRM

OPEN_STATUSES = ("previewed", "review_required", "approved", "failed")


class InvalidTransition(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def idempotency_key(lead_id: str, fingerprint: str, policy_version: str) -> str:
    return hashlib.sha256(f"{lead_id}|{fingerprint}|{policy_version}".encode()).hexdigest()[:32]


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._con() as con:
            con.executescript(
                """
                create table if not exists cases (
                    case_id text primary key,
                    lead_id text not null,
                    idempotency_key text not null unique,
                    input_fingerprint text not null,
                    policy_version text not null,
                    decision_json text not null,
                    rule_id text not null,
                    final_destination text not null,
                    status text not null,
                    attempts integer not null default 0,
                    times_seen integer not null default 1,
                    error text,
                    crm_action_id text,
                    approved_by text,
                    created_at text not null,
                    updated_at text not null
                );
                create index if not exists cases_by_lead on cases (lead_id);
                create table if not exists case_events (
                    id integer primary key autoincrement,
                    case_id text not null,
                    event text not null,
                    detail text,
                    at text not null
                );
                """
            )

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _event(con: sqlite3.Connection, case_id: str, event: str, detail: str = "") -> None:
        con.execute("insert into case_events (case_id, event, detail, at) values (?, ?, ?, ?)",
                    (case_id, event, detail, _now()))

    # ---- reads -------------------------------------------------------------

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self._con() as con:
            row = con.execute("select * from cases where case_id = ?", (case_id,)).fetchone()
            if not row:
                return None
            case = dict(row)
            case["decision"] = json.loads(case.pop("decision_json"))
            case["events"] = [dict(e) for e in con.execute(
                "select event, detail, at from case_events where case_id = ? order by id", (case_id,))]
            return case

    def cases_for_lead(self, lead_id: str) -> list[dict[str, Any]]:
        with self._con() as con:
            ids = [r[0] for r in con.execute(
                "select case_id from cases where lead_id = ? order by created_at, rowid", (lead_id,))]
        return [self.get(i) for i in ids]

    def find_by_key(self, key: str) -> dict[str, Any] | None:
        with self._con() as con:
            row = con.execute("select case_id from cases where idempotency_key = ?", (key,)).fetchone()
        return self.get(row[0]) if row else None

    # ---- writes ------------------------------------------------------------

    def record(self, lead: dict[str, Any], decision: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Store a decision. Returns (case, created). Re-running with unchanged input returns the same case."""
        fp = input_fingerprint(lead)
        key = idempotency_key(lead["lead_id"], fp, decision["policy_version"])
        con = self._con()
        try:
            con.execute("begin immediate")
            existing = con.execute("select case_id from cases where idempotency_key = ?", (key,)).fetchone()
            if existing:
                con.execute("update cases set times_seen = times_seen + 1, updated_at = ? where case_id = ?",
                            (_now(), existing[0]))
                self._event(con, existing[0], "seen_again", "same input and policy version; no new case")
                con.execute("commit")
                return self.get(existing[0]), False

            case_id = f"case_{uuid.uuid4().hex[:12]}"
            for old in con.execute(
                f"select case_id, input_fingerprint, policy_version from cases "
                f"where lead_id = ? and status in ({','.join('?' * len(OPEN_STATUSES))})",
                (lead["lead_id"], *OPEN_STATUSES),
            ).fetchall():
                why = []
                if old["input_fingerprint"] != fp:
                    why.append("lead or account data changed")
                if old["policy_version"] != decision["policy_version"]:
                    why.append(f"policy {old['policy_version']} -> {decision['policy_version']}")
                con.execute("update cases set status = 'superseded', updated_at = ? where case_id = ?",
                            (_now(), old["case_id"]))
                self._event(con, old["case_id"], "superseded", f"by {case_id}: {'; '.join(why)}")

            status = {"duplicate": "duplicate"}.get(decision["status"],
                                                     "review_required" if decision["needs_review"] else "previewed")
            con.execute(
                "insert into cases (case_id, lead_id, idempotency_key, input_fingerprint, policy_version, "
                "decision_json, rule_id, final_destination, status, created_at, updated_at) "
                "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (case_id, lead["lead_id"], key, fp, decision["policy_version"], json.dumps(decision),
                 decision["rule_id"], decision["queue_or_owner"], status, _now(), _now()),
            )
            self._event(con, case_id, "created", f"{decision['rule_id']} -> {decision['queue_or_owner']}")
            con.execute("commit")
            return self.get(case_id), True
        except BaseException:
            con.execute("rollback")
            raise
        finally:
            con.close()

    def _transition(self, case_id: str, allowed: tuple[str, ...], new_status: str, event: str,
                    detail: str = "", bump_attempts: bool = False, **fields: Any) -> dict[str, Any]:
        con = self._con()
        try:
            con.execute("begin immediate")
            row = con.execute("select status from cases where case_id = ?", (case_id,)).fetchone()
            if not row:
                raise InvalidTransition(f"unknown case {case_id}")
            if row["status"] not in allowed:
                raise InvalidTransition(f"{case_id} is {row['status']}; expected one of {', '.join(allowed)}")
            sets = "".join(f", {k} = ?" for k in fields) + (", attempts = attempts + 1" if bump_attempts else "")
            con.execute(f"update cases set status = ?, updated_at = ?{sets} "
                        f"where case_id = ?", (new_status, _now(), *fields.values(), case_id))
            self._event(con, case_id, event, detail)
            con.execute("commit")
        except BaseException:
            con.execute("rollback")
            raise
        finally:
            con.close()
        return self.get(case_id)

    def approve(self, case_id: str, approver: str) -> dict[str, Any]:
        return self._transition(case_id, ("previewed",), "approved", "approved", f"by {approver}",
                                approved_by=approver)

    def resolve(self, case_id: str, owner_or_queue: str, approver: str, note: str) -> dict[str, Any]:
        """A human settles a review case by choosing the destination. The policy decision is kept for the audit trail."""
        if not note.strip():
            raise InvalidTransition("a note explaining the override is required")
        return self._transition(case_id, ("review_required",), "approved", "resolved_by_human",
                                f"{approver} chose {owner_or_queue}: {note}",
                                final_destination=owner_or_queue, approved_by=approver)

    def apply(self, case_id: str, crm: FakeCRM) -> dict[str, Any]:
        """Write the approved destination to the CRM. Safe to call again after a failure or timeout."""
        case = self.get(case_id)
        if case and case["status"] == "applied":
            return case  # already done; never write twice
        # 'applying' is allowed so a worker that died mid-write can be retried;
        # the CRM idempotency key turns that retry into a no-op if the first write landed.
        case = self._transition(case_id, ("approved", "failed", "applying"), "applying", "apply_started",
                                bump_attempts=True)
        try:
            action_id = crm.assign_owner(case["lead_id"], case["final_destination"], case["idempotency_key"])
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, then re-raised
            self._transition(case_id, ("applying",), "failed", "apply_failed", repr(exc), error=repr(exc))
            raise
        return self._transition(case_id, ("applying",), "applied", "applied", action_id,
                                crm_action_id=action_id, error=None)
