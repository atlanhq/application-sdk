"""Unit tests for the last-sync details primitive (BLDX-1229).

Covers the four interesting paths through ``resolve_last_sync_details`` —
top-level workflow, child workflow (where the AE-assigned id is exposed via
``parent_workflow_id``), no execution context (tests / CLI), and explicit
overrides — plus the two dict-stamping wrappers.
"""

from __future__ import annotations

import pytest

from application_sdk.observability import (
    CorrelationContext,
    ExecutionContext,
    set_correlation_context,
    set_execution_context,
)
from application_sdk.transformers.common.last_sync import (
    LAST_SYNC_RUN,
    LAST_SYNC_RUN_AT,
    LAST_SYNC_WORKFLOW_NAME,
    LastSyncDetails,
    resolve_last_sync_details,
    set_last_sync_details,
    set_last_sync_details_bulk,
)


@pytest.fixture(autouse=True)
def _reset_contexts():
    """Always start each test with empty contexts.

    The SDK's execution / correlation context lives in ContextVars; without
    a reset, leakage from prior tests would make these assertions flaky.
    """
    set_execution_context(ExecutionContext())
    set_correlation_context(CorrelationContext())
    yield
    set_execution_context(ExecutionContext())
    set_correlation_context(CorrelationContext())


# ---------------------------------------------------------------------------
# resolve_last_sync_details
# ---------------------------------------------------------------------------


def test_resolve_empty_context_returns_empty_strings_and_current_epoch():
    details = resolve_last_sync_details()
    assert details.run == ""
    assert details.workflow_name == ""
    assert details.run_at_ms > 0  # always populated


def test_resolve_top_level_workflow_uses_workflow_id():
    """Top-level workflow → no parent → workflow_name == workflow_id.

    Fixture uses a realistic AE Temporal workflow_id (a UUID, run-unique).
    """
    set_execution_context(
        ExecutionContext(
            execution_type="workflow",
            workflow_id="4b9eade4-de53-4b69-9010-2446e0a8f85c",
            workflow_run_id="019e2498-87d6-7d99-b345-e00a6dfa8fe2",
        )
    )
    set_correlation_context(
        CorrelationContext(correlation_id="d637c39c-81a0-48b5-bf36-312108e4615c")
    )

    details = resolve_last_sync_details()
    assert details.workflow_name == "4b9eade4-de53-4b69-9010-2446e0a8f85c"
    assert details.run == "d637c39c-81a0-48b5-bf36-312108e4615c"


def test_resolve_child_workflow_prefers_parent_workflow_id():
    """Child workflow → ``parent_workflow_id`` wins over ``workflow_id``.

    Fixture mirrors the BLDX-1229 scenario observed against a real BigQuery
    extract run on a dev tenant: the connector activity runs inside an
    AE-spawned child workflow whose Temporal id is ``<correlation>-extract``;
    the parent is the AE workflow whose id is a fresh UUID per AE run.  The
    v2 pattern (stamp ``input.workflow_id``) produced the child id on assets,
    so operators could not click back to the AE run from an asset.  The
    resolver instead reads ``parent_workflow_id`` and stamps the AE id.
    """
    set_execution_context(
        ExecutionContext(
            execution_type="workflow",
            workflow_id="d637c39c-81a0-48b5-bf36-312108e4615c-extract",
            workflow_run_id="019e2499-c758-7869-9e37-8a50f1d2315c",
            parent_workflow_id="4b9eade4-de53-4b69-9010-2446e0a8f85c",
            parent_run_id="019e2498-87d6-7d99-b345-e00a6dfa8fe2",
        )
    )
    set_correlation_context(
        CorrelationContext(correlation_id="d637c39c-81a0-48b5-bf36-312108e4615c")
    )

    details = resolve_last_sync_details()
    assert details.workflow_name == "4b9eade4-de53-4b69-9010-2446e0a8f85c"
    assert details.run == "d637c39c-81a0-48b5-bf36-312108e4615c"


def test_resolve_explicit_overrides_beat_context():
    set_execution_context(
        ExecutionContext(execution_type="workflow", workflow_id="from-ctx")
    )
    set_correlation_context(CorrelationContext(correlation_id="from-ctx"))

    details = resolve_last_sync_details(
        run="explicit-run",
        workflow_name="explicit-wf",
        run_at_ms=1234567890,
    )
    assert details.run == "explicit-run"
    assert details.workflow_name == "explicit-wf"
    assert details.run_at_ms == 1234567890


# ---------------------------------------------------------------------------
# set_last_sync_details
# ---------------------------------------------------------------------------


def test_set_stamps_camelcase_keys_under_attributes():
    asset: dict = {"typeName": "Table"}
    out = set_last_sync_details(
        asset,
        run="r",
        workflow_name="w",
        run_at_ms=42,
    )
    assert out is asset  # mutates in place
    assert out["attributes"][LAST_SYNC_RUN] == "r"
    assert out["attributes"][LAST_SYNC_WORKFLOW_NAME] == "w"
    assert out["attributes"][LAST_SYNC_RUN_AT] == 42


def test_set_creates_missing_attributes_key():
    asset: dict = {}
    set_last_sync_details(asset, run="r", workflow_name="w", run_at_ms=42)
    assert "attributes" in asset
    assert asset["attributes"][LAST_SYNC_RUN] == "r"


def test_set_preserves_existing_attributes():
    asset = {"attributes": {"name": "Customers", "rowCount": 100}}
    set_last_sync_details(asset, run="r", workflow_name="w", run_at_ms=42)
    assert asset["attributes"]["name"] == "Customers"
    assert asset["attributes"]["rowCount"] == 100
    assert asset["attributes"][LAST_SYNC_RUN_AT] == 42


def test_set_skips_empty_run_and_workflow_name_but_always_writes_run_at():
    """Empty resolutions don't get written — operator dashboards filter on
    presence, so writing ``""`` would be worse than omitting.  ``runAt`` is
    always populated (clock is always available) so it's always written."""
    asset: dict = {}
    set_last_sync_details(asset, run="", workflow_name="", run_at_ms=42)
    attrs = asset.get("attributes", {})
    assert LAST_SYNC_RUN not in attrs
    assert LAST_SYNC_WORKFLOW_NAME not in attrs
    assert attrs[LAST_SYNC_RUN_AT] == 42


def test_set_details_param_wins_over_individual_overrides():
    asset: dict = {}
    details = LastSyncDetails(run="from-details", workflow_name="wf", run_at_ms=1)
    set_last_sync_details(
        asset,
        details=details,
        run="ignored",
        workflow_name="ignored",
        run_at_ms=999,
    )
    assert asset["attributes"][LAST_SYNC_RUN] == "from-details"
    assert asset["attributes"][LAST_SYNC_RUN_AT] == 1


# ---------------------------------------------------------------------------
# set_last_sync_details_bulk
# ---------------------------------------------------------------------------


def test_bulk_applies_same_values_to_every_asset():
    """Every asset in the batch carries identical lastSync* values — the
    resolver runs once, not per asset, so the timestamp is stable across
    the batch."""
    ae_wf_id = "4b9eade4-de53-4b69-9010-2446e0a8f85c"
    corr_id = "d637c39c-81a0-48b5-bf36-312108e4615c"
    set_execution_context(
        ExecutionContext(execution_type="workflow", workflow_id=ae_wf_id)
    )
    set_correlation_context(CorrelationContext(correlation_id=corr_id))

    assets = [{"typeName": "Table"}, {"typeName": "Column"}, {"typeName": "Schema"}]
    out = set_last_sync_details_bulk(assets)

    assert len(out) == 3
    first_run_at = out[0]["attributes"][LAST_SYNC_RUN_AT]
    for asset in out:
        assert asset["attributes"][LAST_SYNC_RUN] == corr_id
        assert asset["attributes"][LAST_SYNC_WORKFLOW_NAME] == ae_wf_id
        # Same timestamp across the whole batch — proves the resolver
        # didn't fire per asset.
        assert asset["attributes"][LAST_SYNC_RUN_AT] == first_run_at


def test_bulk_accepts_explicit_overrides():
    out = set_last_sync_details_bulk(
        [{} for _ in range(3)],
        run="explicit",
        workflow_name="explicit-wf",
        run_at_ms=7,
    )
    for asset in out:
        assert asset["attributes"][LAST_SYNC_RUN] == "explicit"
        assert asset["attributes"][LAST_SYNC_WORKFLOW_NAME] == "explicit-wf"
        assert asset["attributes"][LAST_SYNC_RUN_AT] == 7
