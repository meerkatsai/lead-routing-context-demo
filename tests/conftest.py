import os
import subprocess
import sys
from pathlib import Path

import pytest

from lead_routing.config import REPO_ROOT, load_settings


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory) -> Path:
    """Build the dbt model once into a temp folder so tests never touch your local warehouse.

    dbt runs in a subprocess: in-process, dbt-duckdb keeps its write connection open,
    and DuckDB then refuses the read-only connections the app makes.
    """
    root = tmp_path_factory.mktemp("warehouse")
    os.environ["LEAD_ROUTING_DUCKDB"] = str(root / "lead_routing.duckdb")
    project = str(REPO_ROOT / "dbt_project")
    res = subprocess.run(
        [str(Path(sys.executable).with_name("dbt")), "build", "--project-dir", project, "--profiles-dir", project,
         "--target-path", str(root / "dbt_target"), "--log-path", str(root / "logs"), "--quiet"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, f"dbt build failed:\n{res.stdout}\n{res.stderr}"
    return root


@pytest.fixture
def settings(warehouse, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAD_ROUTING_LEDGER", str(tmp_path / "ledger.sqlite"))
    monkeypatch.setenv("LEAD_ROUTING_FAKE_CRM", str(tmp_path / "crm.sqlite"))
    return load_settings()


def base_lead(**overrides):
    """A clean, normalized lead that routes by territory. Tests change one thing at a time."""
    lead = {
        "lead_id": "T-1", "email_normalized": "a@example.com", "company_domain_normalized": "example.com",
        "company_domain_source": "provided", "country_code": "US", "employee_count": 50,
        "duplicate_of_lead_id": None, "account_id": None, "is_existing_customer": False,
        "account_owner_id": None, "open_opportunity_count": 0, "open_opportunity_ids": None,
        "open_opportunity_owner_ids": None, "territory_owner_id": "own_maya",
        "missing_fields": "", "missing_required_data": False,
    }
    lead.update(overrides)
    return lead
