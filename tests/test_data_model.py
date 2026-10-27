"""Checks that the dbt model and policy together produce the expected outcome for every synthetic lead."""

import csv

import pytest

from lead_routing import context
from lead_routing.config import REPO_ROOT
from lead_routing.policy import route_lead

with open(REPO_ROOT / "data" / "expected_outcomes.csv", newline="") as fh:
    EXPECTED = list(csv.DictReader(fh))


@pytest.mark.parametrize("row", EXPECTED, ids=[r["lead_id"] for r in EXPECTED])
def test_expected_outcome(settings, row):
    d = route_lead(context.get_normalized_lead(row["lead_id"], settings), settings)
    assert (d["rule_id"], d["queue_or_owner"]) == (row["expected_rule_id"], row["expected_destination"]), row["why"]


def test_normalization(settings):
    l13 = context.get_normalized_lead("L-013", settings)
    assert (l13["company_domain_normalized"], l13["country_code"]) == ("orbitkiln.com", "US")
    l08 = context.get_normalized_lead("L-008", settings)
    assert l08["employee_count"] == 1200
    l15 = context.get_normalized_lead("L-015", settings)
    assert (l15["company_domain_normalized"], l15["company_domain_source"]) == ("ferncastle.co", "email")
    l07 = context.get_normalized_lead("L-007", settings)
    assert l07["company_domain_normalized"] is None  # gmail.com is never treated as a company


def test_fingerprint_ignores_build_time(settings):
    lead = context.get_normalized_lead("L-001", settings)
    later = dict(lead, built_at="2099-01-01T00:00:00")
    assert context.input_fingerprint(lead) == context.input_fingerprint(later)
    assert context.input_fingerprint(lead) != context.input_fingerprint(dict(lead, country_code="GB"))
