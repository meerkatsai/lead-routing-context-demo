"""Unit tests for rule behaviour and precedence. No database needed."""

import pytest

from conftest import base_lead
from lead_routing.config import Settings
from lead_routing.policy import RULES, route_lead

S = Settings(duckdb_path=None, ledger_path=None, crm_path=None, policy_version="test", enterprise_threshold=200)


def route(**kw):
    return route_lead(base_lead(**kw), S)


def test_clean_lead_routes_by_territory():
    d = route()
    assert (d["rule_id"], d["queue_or_owner"], d["needs_review"]) == ("TERRITORY_OWNER", "own_maya", False)


def test_existing_opportunity_owner_kept_even_with_missing_territory():
    d = route(open_opportunity_count=1, open_opportunity_owner_ids="own_lukas", country_code=None,
              territory_owner_id=None, missing_fields="country", missing_required_data=True)
    assert (d["rule_id"], d["queue_or_owner"]) == ("EXISTING_ACCOUNT_OWNER", "own_lukas")


def test_existing_customer_beats_enterprise_threshold():
    d = route(is_existing_customer=True, account_owner_id="own_oliver", employee_count=5000)
    assert (d["rule_id"], d["queue_or_owner"]) == ("EXISTING_ACCOUNT_OWNER", "own_oliver")


def test_existing_customer_without_owner_goes_to_review():
    d = route(is_existing_customer=True)
    assert (d["rule_id"], d["needs_review"]) == ("EXISTING_OWNER_MISSING", True)


def test_conflicting_owners_go_to_review():
    d = route(is_existing_customer=True, account_owner_id="own_oliver",
              open_opportunity_count=1, open_opportunity_owner_ids="own_maya")
    assert (d["rule_id"], d["needs_review"]) == ("OWNER_CONFLICT", True)


def test_same_owner_on_account_and_opportunity_is_not_a_conflict():
    d = route(is_existing_customer=True, account_owner_id="own_priya",
              open_opportunity_count=1, open_opportunity_owner_ids="own_priya")
    assert (d["rule_id"], d["queue_or_owner"]) == ("EXISTING_ACCOUNT_OWNER", "own_priya")


@pytest.mark.parametrize("field", ["email", "company_domain", "country"])
def test_missing_required_field_goes_to_review_instead_of_guessing(field):
    d = route(missing_fields=field, missing_required_data=True)
    assert (d["rule_id"], d["needs_review"]) == ("MISSING_REQUIRED_DATA", True)
    assert d["evidence"]["missing_fields"] == [field]


@pytest.mark.parametrize("employees,rule", [(199, "TERRITORY_OWNER"), (200, "ENTERPRISE_THRESHOLD"),
                                            (201, "ENTERPRISE_THRESHOLD")])
def test_enterprise_threshold_boundary(employees, rule):
    assert route(employee_count=employees)["rule_id"] == rule


def test_unknown_size_is_flagged_not_treated_as_small_silently():
    d = route(employee_count=None)
    assert d["rule_id"] == "TERRITORY_OWNER"
    assert "employee_count_unknown" in d["data_quality_flags"]
    assert "not evaluated" in d["reason"]


def test_no_territory_mapping_goes_to_review():
    d = route(country_code="FR", territory_owner_id=None)
    assert (d["rule_id"], d["needs_review"]) == ("NO_SAFE_MATCH", True)


def test_duplicate_wins_over_everything():
    d = route(duplicate_of_lead_id="L-001", is_existing_customer=True, account_owner_id="own_oliver")
    assert (d["rule_id"], d["queue_or_owner"], d["status"]) == ("DUPLICATE_LEAD", "L-001", "duplicate")


def test_policy_is_deterministic():
    lead = base_lead(employee_count=None, country_code="FR", territory_owner_id=None)
    assert route_lead(lead, S) == route_lead(lead, S)


def test_every_returned_rule_is_documented():
    documented = {r["rule_id"] for r in RULES}
    cases = [route(), route(employee_count=500), route(country_code="FR", territory_owner_id=None),
             route(missing_fields="email", missing_required_data=True), route(duplicate_of_lead_id="X"),
             route(is_existing_customer=True), route(is_existing_customer=True, account_owner_id="a"),
             route(is_existing_customer=True, account_owner_id="a", open_opportunity_owner_ids="b")]
    assert {c["rule_id"] for c in cases} == documented


def test_missing_data_beats_enterprise_threshold():
    d = route(employee_count=5000, missing_fields="country", missing_required_data=True, territory_owner_id=None)
    assert d["rule_id"] == "MISSING_REQUIRED_DATA"


def test_duplicate_beats_missing_data():
    d = route(duplicate_of_lead_id="L-001", missing_fields="country", missing_required_data=True)
    assert d["rule_id"] == "DUPLICATE_LEAD"
