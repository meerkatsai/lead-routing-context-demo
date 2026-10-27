"""The MCP tools, called through a real in-process MCP client."""

import asyncio
import json

from mcp import Client

from lead_routing.mcp_server import mcp


def call(name, args=None):
    async def run():
        async with Client(mcp) as client:
            res = await client.call_tool(name, args or {})
            return res.structured_content or json.loads(res.content[0].text)
    return asyncio.run(run())


def tools():
    async def run():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools
    return asyncio.run(run())


def test_every_tool_is_marked_read_only(settings):
    listed = tools()
    assert {t.name for t in listed} == {"list_leads_for_routing", "get_lead_context", "preview_route",
                                        "preview_route_with_country_hint", "get_routing_case",
                                        "explain_routing_policy"}
    for t in listed:
        assert t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False, t.name


def test_preview_route_returns_structured_decision(settings):
    d = call("preview_route", {"lead_id": "L-002"})
    assert (d["rule_id"], d["queue_or_owner"]) == ("EXISTING_ACCOUNT_OWNER", "own_oliver")
    assert d["ledger"]["status"] == "not_recorded"
    assert not settings.crm_path.exists()  # nothing was written


def test_unknown_lead_is_a_clear_error(settings):
    assert call("preview_route", {"lead_id": "L-999"}) == {"error": "lead_not_found", "lead_id": "L-999"}
    assert call("get_lead_context", {"lead_id": "L-999"})["error"] == "lead_not_found"


def test_list_is_bounded(settings):
    assert len(call("list_leads_for_routing", {"limit": 5000})["leads"]) == 15
    assert len(call("list_leads_for_routing", {"limit": 3})["leads"]) == 3


def test_notes_are_labelled_untrusted(settings):
    ctx = call("get_lead_context", {"lead_id": "L-006"})
    assert "Pune" in ctx["notes_untrusted"]


def test_country_hint_is_validated_and_always_reviewed(settings):
    ok = call("preview_route_with_country_hint",
              {"lead_id": "L-006", "country_code": "in", "evidence": "Our team is based in Pune"})
    assert ok["accepted"] and ok["decision"]["queue_or_owner"] == "own_priya"
    assert ok["decision"]["needs_review"] is True
    assert ok["decision"]["input_overrides"]["country_code"]["source"] == "model_hint"

    bad = call("preview_route_with_country_hint", {"lead_id": "L-006", "country_code": "XX", "evidence": "a long quote"})
    assert not bad["accepted"]
    no_evidence = call("preview_route_with_country_hint", {"lead_id": "L-006", "country_code": "IN", "evidence": ""})
    assert not no_evidence["accepted"]
    has_country = call("preview_route_with_country_hint",
                       {"lead_id": "L-001", "country_code": "GB", "evidence": "they said London"})
    assert not has_country["accepted"]


def test_get_routing_case_before_any_run(settings):
    assert call("get_routing_case", {"case_or_lead_id": "L-001"})["error"] == "no_cases_yet"
