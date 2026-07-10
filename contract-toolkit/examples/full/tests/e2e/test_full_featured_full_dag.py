# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
# isort: skip_file
"""Full-DAG (AGENT / SDR) e2e test for full-featured.

Submits the tenant's full system-apps DAG (extract → qi → publish →
lineage) against a CI-side worker that runs the connector code under test,
then asserts assets + lineage land in Atlas. Extract is routed to the CI
worker's Temporal queue via agent-json agent-name; artifacts move through
the shared object store. Skips gracefully unless the harness env is set::

    ATLAN_BASE_URL=... ATLAN_API_KEY=... GITHUB_RUN_ID=$(date +%s) uv run pytest tests/e2e/ -v
"""

from __future__ import annotations

import os

import pytest

if not os.environ.get("ATLAN_BASE_URL") or not os.environ.get("ATLAN_API_KEY"):
    pytest.skip(
        "Full-DAG e2e harness needs ATLAN_BASE_URL + ATLAN_API_KEY",
        allow_module_level=True,
    )

try:
    from application_sdk.testing.e2e import RunMode
    from application_sdk.testing.e2e.payload import DatabaseSpec
    from app.generated._e2e_base import FullFeaturedGeneratedE2EBase
    from app.generated._e2e_credential import FullFeaturedAgentCredentialBody
except ImportError as _exc:
    pytest.skip(
        f"SDK does not yet export new e2e harness: {_exc}", allow_module_level=True
    )


class TestFullFeaturedFullDAG(FullFeaturedGeneratedE2EBase):
    # AGENT mode: the connector runs on the CI-side worker (SDR path).
    mode = RunMode.AGENT

    include_filter = "^public\\.e2e_.*$"
    exclude_filter = ""

    # Atlas inventory floors for the hermetic seed (>=; conservative to
    # ride out transient indexer lag).
    expected_min_asset_counts = {
        "Database": 1,
        "Schema": 1,
        "Table": 2,
        "Column": 8,
    }
    expect_lineage = True

    def database_spec(self) -> DatabaseSpec:
        # host is the CI-side source (compose service name). In AGENT mode
        # username/password are not sent on the wire — the agent resolves
        # creds from its secret store via SDR_<APP>_* ref keys.
        return DatabaseSpec(
            host="postgres",
            port=5432,
            username="e2e_user",
            password="e2e_pass",
            connector_config_name="atlan-connectors-full-featured",
        )

    def _credential_body(self) -> FullFeaturedAgentCredentialBody:
        # Lightweight AGENT body — no inline host/user/pass (those would make
        # the orchestrator treat the submit as DIRECT and skip credential
        # creation). The GITHUB_RUN_ATTEMPT suffix avoids a unique-name
        # collision when a run is re-attempted.
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        return FullFeaturedAgentCredentialBody(
            name=f"default-{self.connector_short_name}-{self.run_id}-{attempt}",
        )
