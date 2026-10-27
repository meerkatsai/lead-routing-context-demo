"""Command line for building context, routing leads and moving cases through the workflow."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any

from . import context, policy
from .config import REPO_ROOT, load_settings
from .crm_stub import FakeCRM
from .workflow import InvalidTransition, Ledger


def _table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c, "") or "")) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "") or "").ljust(widths[c]) for c in cols))


def cmd_build(_: argparse.Namespace) -> int:
    from dbt.cli.main import dbtRunner

    s = load_settings()
    s.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["LEAD_ROUTING_DUCKDB"] = str(s.duckdb_path)
    project = str(REPO_ROOT / "dbt_project")
    res = dbtRunner().invoke(["build", "--project-dir", project, "--profiles-dir", project])
    return 0 if res.success else 1


def cmd_list(a: argparse.Namespace) -> int:
    rows = context.list_leads(a.limit)
    _table(rows, ["lead_id", "company_domain_normalized", "country_code", "employee_count",
                  "account_id", "missing_fields", "duplicate_of_lead_id"])
    return 0


def cmd_preview(a: argparse.Namespace) -> int:
    lead = context.get_normalized_lead(a.lead_id)
    if lead is None:
        print(f"lead {a.lead_id} not found", file=sys.stderr)
        return 1
    print(json.dumps(policy.route_lead(lead), indent=2))
    return 0


def cmd_route_all(_: argparse.Namespace) -> int:
    s = load_settings()
    ledger = Ledger(s.ledger_path)
    out = []
    for lead in context.list_leads(100, s):
        decision = policy.route_lead(lead, s)
        case, created = ledger.record(lead, decision)
        out.append({"lead_id": lead["lead_id"], "rule_id": decision["rule_id"],
                    "destination": decision["queue_or_owner"], "case_status": case["status"],
                    "case_id": case["case_id"], "ledger": "new case" if created else "existing case"})
    _table(out, ["lead_id", "rule_id", "destination", "case_status", "case_id", "ledger"])
    return 0


def cmd_case(a: argparse.Namespace) -> int:
    ledger = Ledger(load_settings().ledger_path)
    found = ledger.get(a.id) if a.id.startswith("case_") else ledger.cases_for_lead(a.id)
    if not found:
        print(f"nothing found for {a.id}", file=sys.stderr)
        return 1
    print(json.dumps(found, indent=2))
    return 0


def _transition(fn) -> int:
    try:
        case = fn()
    except InvalidTransition as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"{case['case_id']}: {case['status']} -> {case['final_destination']}"
          + (f" (crm action {case['crm_action_id']})" if case.get("crm_action_id") else ""))
    return 0


def cmd_approve(a: argparse.Namespace) -> int:
    return _transition(lambda: Ledger(load_settings().ledger_path).approve(a.case_id, a.by))


def cmd_resolve(a: argparse.Namespace) -> int:
    return _transition(lambda: Ledger(load_settings().ledger_path).resolve(a.case_id, a.owner, a.by, a.note))


def cmd_apply(a: argparse.Namespace) -> int:
    s = load_settings()
    crm = FakeCRM(s.crm_path)
    crm.fail_next = int(a.simulate_outage)
    crm.lose_next_response = int(a.simulate_timeout)
    try:
        return _transition(lambda: Ledger(s.ledger_path).apply(a.case_id, crm))
    except Exception as exc:  # noqa: BLE001
        print(f"apply failed and was recorded on the case: {exc!r}. Run apply again to retry.", file=sys.stderr)
        return 3


def cmd_crm_writes(_: argparse.Namespace) -> int:
    writes = FakeCRM(load_settings().crm_path).writes()
    _table([{"lead_id": w[0], "owner_or_queue": w[1], "action_id": w[2]} for w in writes] or [{}],
           ["lead_id", "owner_or_queue", "action_id"])
    return 0


def cmd_eval(_: argparse.Namespace) -> int:
    s = load_settings()
    with open(REPO_ROOT / "data" / "expected_outcomes.csv", newline="") as fh:
        expected = {r["lead_id"]: r for r in csv.DictReader(fh)}
    rows, correct, wrong_assign, review = [], 0, 0, 0
    for lead_id, exp in expected.items():
        lead = context.get_normalized_lead(lead_id, s)
        d = policy.route_lead(lead, s)
        ok = d["rule_id"] == exp["expected_rule_id"] and d["queue_or_owner"] == exp["expected_destination"]
        correct += ok
        review += d["needs_review"]
        wrong_assign += (not ok) and d["status"] == "assigned"
        rows.append({"lead_id": lead_id, "expected": f"{exp['expected_rule_id']} -> {exp['expected_destination']}",
                     "got": f"{d['rule_id']} -> {d['queue_or_owner']}", "match": "yes" if ok else "NO"})
    _table(rows, ["lead_id", "expected", "got", "match"])
    n = len(expected)
    print(f"\naccuracy {correct}/{n}   false assignments {wrong_assign}   review rate {review / n:.0%}")
    return 0 if correct == n else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="lead-routing", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="seed synthetic data and build the dbt routing model").set_defaults(fn=cmd_build)
    sp = sub.add_parser("list", help="show normalized leads")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(fn=cmd_list)
    sp = sub.add_parser("preview", help="show the policy decision for one lead (writes nothing)")
    sp.add_argument("lead_id")
    sp.set_defaults(fn=cmd_preview)
    sub.add_parser("route-all", help="route every lead and record cases in the ledger").set_defaults(fn=cmd_route_all)
    sp = sub.add_parser("case", help="show a case by case ID, or all cases for a lead ID")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_case)
    sp = sub.add_parser("approve", help="approve a previewed case")
    sp.add_argument("case_id")
    sp.add_argument("--by", required=True)
    sp.set_defaults(fn=cmd_approve)
    sp = sub.add_parser("resolve", help="settle a review case by choosing the owner or queue")
    sp.add_argument("case_id")
    sp.add_argument("--owner", required=True)
    sp.add_argument("--by", required=True)
    sp.add_argument("--note", required=True)
    sp.set_defaults(fn=cmd_resolve)
    sp = sub.add_parser("apply", help="write an approved case to the fake CRM")
    sp.add_argument("case_id")
    sp.add_argument("--simulate-outage", action="store_true", help="CRM fails before writing")
    sp.add_argument("--simulate-timeout", action="store_true", help="CRM writes, then the response is lost")
    sp.set_defaults(fn=cmd_apply)
    sub.add_parser("crm-writes", help="show what the fake CRM received").set_defaults(fn=cmd_crm_writes)
    sub.add_parser("eval", help="compare decisions with data/expected_outcomes.csv").set_defaults(fn=cmd_eval)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except context.WarehouseNotBuilt as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
