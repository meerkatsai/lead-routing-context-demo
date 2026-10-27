"""Workflow state: re-runs, superseding, approvals and safe CRM retries."""

import dataclasses

import pytest

from lead_routing import context
from lead_routing.crm_stub import CRMUnavailable, FakeCRM
from lead_routing.policy import route_lead
from lead_routing.workflow import InvalidTransition, Ledger


@pytest.fixture
def ledger(settings):
    return Ledger(settings.ledger_path)


@pytest.fixture
def crm(settings):
    return FakeCRM(settings.crm_path)


def record(ledger, settings, lead_id="L-001", lead=None):
    lead = lead or context.get_normalized_lead(lead_id, settings)
    return ledger.record(lead, route_lead(lead, settings))


def test_rerun_with_unchanged_input_reuses_the_case(ledger, settings):
    first, created1 = record(ledger, settings)
    second, created2 = record(ledger, settings)
    assert created1 and not created2
    assert first["case_id"] == second["case_id"]
    assert second["times_seen"] == 2
    assert len(ledger.cases_for_lead("L-001")) == 1


def test_changed_input_supersedes_with_a_reason(ledger, settings):
    old, _ = record(ledger, settings)
    lead = dict(context.get_normalized_lead("L-001", settings), country_code="GB", territory_owner_id="own_oliver")
    new, created = record(ledger, settings, lead=lead)
    assert created and new["final_destination"] == "own_oliver"
    old = ledger.get(old["case_id"])
    assert old["status"] == "superseded"
    assert "data changed" in old["events"][-1]["detail"]


def test_policy_version_change_supersedes_with_a_reason(ledger, settings):
    old, _ = record(ledger, settings)
    v2 = dataclasses.replace(settings, policy_version="v2")
    lead = context.get_normalized_lead("L-001", settings)
    new, created = ledger.record(lead, route_lead(lead, v2))
    assert created and new["policy_version"] == "v2"
    assert "policy v1 -> v2" in ledger.get(old["case_id"])["events"][-1]["detail"]


def test_apply_requires_approval(ledger, settings, crm):
    case, _ = record(ledger, settings)
    with pytest.raises(InvalidTransition):
        ledger.apply(case["case_id"], crm)
    assert crm.writes() == []


def test_apply_twice_writes_once(ledger, settings, crm):
    case, _ = record(ledger, settings)
    ledger.approve(case["case_id"], "test")
    a = ledger.apply(case["case_id"], crm)
    b = ledger.apply(case["case_id"], crm)
    assert a["status"] == b["status"] == "applied"
    assert a["crm_action_id"] == b["crm_action_id"]
    assert len(crm.writes()) == 1


def test_outage_is_recorded_and_retry_succeeds(ledger, settings, crm):
    case, _ = record(ledger, settings)
    ledger.approve(case["case_id"], "test")
    crm.fail_next = 1
    with pytest.raises(CRMUnavailable):
        ledger.apply(case["case_id"], crm)
    failed = ledger.get(case["case_id"])
    assert failed["status"] == "failed" and "503" in failed["error"]
    done = ledger.apply(case["case_id"], crm)
    assert done["status"] == "applied" and done["attempts"] == 2
    assert len(crm.writes()) == 1


def test_lost_response_retry_does_not_duplicate_the_write(ledger, settings, crm):
    case, _ = record(ledger, settings)
    ledger.approve(case["case_id"], "test")
    crm.lose_next_response = 1
    with pytest.raises(TimeoutError):
        ledger.apply(case["case_id"], crm)
    assert len(crm.writes()) == 1  # the write landed even though we never heard back
    done = ledger.apply(case["case_id"], crm)
    assert done["status"] == "applied"
    assert len(crm.writes()) == 1


def test_review_case_needs_a_human_resolution_with_a_note(ledger, settings, crm):
    case, _ = record(ledger, settings, "L-005")
    assert case["status"] == "review_required"
    with pytest.raises(InvalidTransition):
        ledger.approve(case["case_id"], "test")
    with pytest.raises(InvalidTransition):
        ledger.resolve(case["case_id"], "own_maya", "test", note="  ")
    resolved = ledger.resolve(case["case_id"], "own_maya", "rev.ops", note="Negotiation-stage opp owner wins")
    assert (resolved["status"], resolved["final_destination"]) == ("approved", "own_maya")
    assert resolved["decision"]["rule_id"] == "OWNER_CONFLICT"  # the original decision stays on record
    assert ledger.apply(case["case_id"], crm)["status"] == "applied"


def test_duplicates_are_terminal(ledger, settings, crm):
    case, _ = record(ledger, settings, "L-012")
    assert case["status"] == "duplicate"
    with pytest.raises(InvalidTransition):
        ledger.approve(case["case_id"], "test")
