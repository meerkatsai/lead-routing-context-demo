"""Read normalized leads and their business context from the dbt-built DuckDB file."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import duckdb

from .config import Settings, load_settings

# Columns that change on every dbt run and must not affect the input fingerprint.
_VOLATILE = {"built_at"}


class WarehouseNotBuilt(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _query(sql: str, params: list[Any], settings: Settings) -> list[dict[str, Any]]:
    if not settings.duckdb_path.exists():
        raise WarehouseNotBuilt(
            f"{settings.duckdb_path} not found. Run `lead-routing build` first."
        )
    # Open read-only per call and close straight away, so `dbt build` can take the write lock.
    con = duckdb.connect(str(settings.duckdb_path), read_only=True)
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [{c: _jsonable(v) for c, v in zip(cols, row)} for row in cur.fetchall()]
    finally:
        con.close()


def get_normalized_lead(lead_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    rows = _query("select * from lead_routing_base where lead_id = ?", [lead_id], settings or load_settings())
    return rows[0] if rows else None


def list_leads(limit: int = 20, settings: Settings | None = None) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    return _query("select * from lead_routing_base order by lead_created_at, lead_id limit ?",
                  [limit], settings or load_settings())


def territory_owner_for(country_code: str, settings: Settings | None = None) -> str | None:
    rows = _query("select territory_owner_id from territory_owners where country_code = ?",
                  [country_code], settings or load_settings())
    return rows[0]["territory_owner_id"] if rows else None


def known_country_codes(settings: Settings | None = None) -> set[str]:
    rows = _query("select distinct country_code from country_aliases", [], settings or load_settings())
    return {r["country_code"] for r in rows}


def input_fingerprint(lead: dict[str, Any]) -> str:
    """Stable hash of the inputs the policy saw. A changed fingerprint explains a changed decision."""
    stable = {k: _jsonable(v) for k, v in sorted(lead.items()) if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
