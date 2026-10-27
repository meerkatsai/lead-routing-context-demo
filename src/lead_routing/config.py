"""Paths and policy settings. Every value can be overridden with an environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _path(env: str, default: Path) -> Path:
    value = os.environ.get(env)
    return Path(value).expanduser().resolve() if value else default


@dataclass(frozen=True)
class Settings:
    duckdb_path: Path
    ledger_path: Path
    crm_path: Path
    policy_version: str
    enterprise_threshold: int
    review_queue: str = "revops_review"
    enterprise_queue: str = "enterprise_queue"


def load_settings() -> Settings:
    return Settings(
        duckdb_path=_path("LEAD_ROUTING_DUCKDB", REPO_ROOT / "warehouse" / "lead_routing.duckdb"),
        ledger_path=_path("LEAD_ROUTING_LEDGER", REPO_ROOT / "warehouse" / "ledger.sqlite"),
        crm_path=_path("LEAD_ROUTING_FAKE_CRM", REPO_ROOT / "warehouse" / "fake_crm.sqlite"),
        policy_version=os.environ.get("LEAD_ROUTING_POLICY_VERSION", "v1"),
        # Illustrative. Replace with your company's real segmentation rule.
        enterprise_threshold=int(os.environ.get("LEAD_ROUTING_ENTERPRISE_THRESHOLD", "200")),
    )
