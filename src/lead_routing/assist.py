"""Guardrails for model suggestions.

An assistant can read a lead's free-text notes and suggest a value the policy
needs, such as a country. That suggestion is input, never a decision: it is
checked against an allowed list, the deterministic policy runs on it, and the
result always goes to a human because the value came from a model.
"""

from __future__ import annotations

import re
from typing import Any

from .config import Settings, load_settings
from .context import known_country_codes, territory_owner_for
from .policy import route_lead

_CODE = re.compile(r"^[A-Z]{2}$")


def validate_country_hint(hint: str, evidence: str, settings: Settings | None = None) -> tuple[bool, str]:
    s = settings or load_settings()
    code = (hint or "").strip().upper()
    if not _CODE.match(code):
        return False, f"'{hint}' is not a two-letter country code"
    if code not in known_country_codes(s):
        return False, f"{code} is not in the allowed country list"
    if len((evidence or "").strip()) < 8:
        return False, "quote the text that supports this country as evidence"
    return True, code


def preview_with_country_hint(lead: dict[str, Any], hint: str, evidence: str,
                              settings: Settings | None = None) -> dict[str, Any]:
    s = settings or load_settings()
    ok, value = validate_country_hint(hint, evidence, s)
    if not ok:
        return {"lead_id": lead["lead_id"], "accepted": False, "error": value}
    if lead.get("country_code"):
        return {"lead_id": lead["lead_id"], "accepted": False,
                "error": f"lead already has country {lead['country_code']}; hints only fill missing values"}

    patched = dict(lead)
    patched["country_code"] = value
    patched["territory_owner_id"] = territory_owner_for(value, s)
    missing = [f for f in (lead.get("missing_fields") or "").split(",") if f and f != "country"]
    patched["missing_fields"] = ",".join(missing)
    patched["missing_required_data"] = bool(missing)

    decision = route_lead(patched, s)
    decision["needs_review"] = True  # a model supplied an input, so a human confirms it
    decision["input_overrides"] = {"country_code": {"value": value, "source": "model_hint", "evidence": evidence}}
    return {"lead_id": lead["lead_id"], "accepted": True, "decision": decision}
