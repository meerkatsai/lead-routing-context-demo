"""Deterministic routing policy.

The policy takes one normalized lead (a row of the dbt model `lead_routing_base`)
and returns a structured decision. It has no I/O and no model calls, so ordinary
unit tests can pin down its behaviour, including precedence between rules.

Rules run in this order. The first rule that applies wins.

1. DUPLICATE_LEAD          same person as an earlier lead -> link, don't reassign
2. EXISTING_ACCOUNT_OWNER  customer account or open opportunity with one clear owner
   OWNER_CONFLICT          ... with two or more different owners -> review
   EXISTING_OWNER_MISSING  ... with no owner on record -> review
3. MISSING_REQUIRED_DATA   email, company domain or country missing -> review
4. ENTERPRISE_THRESHOLD    employee count at or above the threshold -> enterprise queue
5. TERRITORY_OWNER         country maps to exactly one territory owner
6. NO_SAFE_MATCH           nothing above applied -> review

The order and the threshold are examples. Encode your own policy, bump the
version when you change it, and add a test for every pair of rules that can
both match the same lead.
"""

from __future__ import annotations

from typing import Any, Mapping

from .config import Settings, load_settings

RULES: list[dict[str, str]] = [
    {"rule_id": "DUPLICATE_LEAD", "outcome": "duplicate",
     "description": "The normalized email matches an earlier lead. Link to it instead of routing again."},
    {"rule_id": "EXISTING_ACCOUNT_OWNER", "outcome": "assigned",
     "description": "The company is a customer or has an open opportunity, and exactly one owner is on record."},
    {"rule_id": "OWNER_CONFLICT", "outcome": "review",
     "description": "An existing relationship exists but the account and opportunities name different owners."},
    {"rule_id": "EXISTING_OWNER_MISSING", "outcome": "review",
     "description": "An existing relationship exists but no owner is on record."},
    {"rule_id": "MISSING_REQUIRED_DATA", "outcome": "review",
     "description": "Email, company domain or country is missing, so the policy does not guess."},
    {"rule_id": "ENTERPRISE_THRESHOLD", "outcome": "queued",
     "description": "Employee count is at or above the configured enterprise threshold."},
    {"rule_id": "TERRITORY_OWNER", "outcome": "assigned",
     "description": "The normalized country maps to a territory owner."},
    {"rule_id": "NO_SAFE_MATCH", "outcome": "review",
     "description": "No rule produced a safe assignment."},
]


def _split(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [v.strip() for v in str(value).split(",") if v.strip()]


def known_owners(lead: Mapping[str, Any]) -> list[str]:
    """Distinct owners named on the matched account and its open opportunities."""
    owners = set(_split(lead.get("open_opportunity_owner_ids")))
    if lead.get("account_owner_id"):
        owners.add(str(lead["account_owner_id"]))
    return sorted(owners)


def route_lead(lead: Mapping[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    s = settings or load_settings()
    flags: list[str] = []
    if lead.get("employee_count") is None:
        flags.append("employee_count_unknown")
    if lead.get("company_domain_source") == "email":
        flags.append("company_domain_from_email")

    def result(status: str, destination: str, rule_id: str, reason: str,
               needs_review: bool = False, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "lead_id": lead["lead_id"],
            "status": status,
            "queue_or_owner": destination,
            "rule_id": rule_id,
            "reason": reason,
            "needs_review": needs_review,
            "policy_version": s.policy_version,
            "evidence": evidence or {},
            "data_quality_flags": flags,
        }

    # 1. Duplicates first, so one person never gets two owners.
    original = lead.get("duplicate_of_lead_id")
    if original:
        return result("duplicate", str(original), "DUPLICATE_LEAD",
                      f"Same normalized email as {original}; linked instead of routed again",
                      evidence={"duplicate_of_lead_id": original})

    # 2. An existing relationship beats every generic rule below it.
    has_relationship = bool(lead.get("is_existing_customer")) or int(lead.get("open_opportunity_count") or 0) > 0
    if has_relationship:
        owners = known_owners(lead)
        evidence = {
            "account_id": lead.get("account_id"),
            "is_existing_customer": bool(lead.get("is_existing_customer")),
            "account_owner_id": lead.get("account_owner_id"),
            "open_opportunity_ids": _split(lead.get("open_opportunity_ids")),
            "open_opportunity_owner_ids": _split(lead.get("open_opportunity_owner_ids")),
        }
        if len(owners) == 1:
            return result("assigned", owners[0], "EXISTING_ACCOUNT_OWNER",
                          "Kept the owner of the existing account/opportunity", evidence=evidence)
        if len(owners) > 1:
            return result("review", s.review_queue, "OWNER_CONFLICT",
                          f"Existing relationship has conflicting owners: {', '.join(owners)}",
                          needs_review=True, evidence=evidence)
        return result("review", s.review_queue, "EXISTING_OWNER_MISSING",
                      "Existing relationship found, but no owner is on record",
                      needs_review=True, evidence=evidence)

    # 3. Don't guess when a field the rules depend on is missing.
    missing = _split(lead.get("missing_fields"))
    if missing or lead.get("missing_required_data"):
        return result("review", s.review_queue, "MISSING_REQUIRED_DATA",
                      f"Required routing fields are missing: {', '.join(missing) or 'unknown'}",
                      needs_review=True, evidence={"missing_fields": missing})

    # 4. Size-based segment. Unknown size is not treated as zero silently: it is flagged.
    employees = lead.get("employee_count")
    if employees is not None and int(employees) >= s.enterprise_threshold:
        return result("queued", s.enterprise_queue, "ENTERPRISE_THRESHOLD",
                      f"Employee count {employees} meets the threshold of {s.enterprise_threshold}",
                      evidence={"employee_count": employees, "threshold": s.enterprise_threshold})

    # 5. Territory.
    owner = lead.get("territory_owner_id")
    if owner:
        reason = f"Assigned by territory mapping for {lead.get('country_code')}"
        if employees is None:
            reason += "; employee count unknown, so the enterprise threshold was not evaluated"
        return result("assigned", str(owner), "TERRITORY_OWNER", reason,
                      evidence={"country_code": lead.get("country_code"), "territory_owner_id": owner})

    # 6. Fallback.
    return result("review", s.review_queue, "NO_SAFE_MATCH",
                  f"No territory owner for country {lead.get('country_code')}",
                  needs_review=True, evidence={"country_code": lead.get("country_code")})
