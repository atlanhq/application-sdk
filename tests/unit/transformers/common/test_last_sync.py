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
    """Top-level workflow → no parent → workflow_name == workflow_id."""
    set_execution_context(
        ExecutionContext(
            execution_type="workflow",
            workflow_id="dbt-AMBSvQPJ",
            workflow_run_id="run-uuid-1",
        )
    )
    set_correlation_context(CorrelationContext(correlation_id="corr-1"))

    details = resolve_last_sync_details()
    assert details.workflow_name == "dbt-AMBSvQPJ"
    assert details.run == "corr-1"


def test_resolve_child_workflow_prefers_parent_workflow_id():
    """Child workflow → ``parent_workflow_id`` wins over ``workflow_id``.

    This is the exact bug Chetan hit in the BLDX-1229 thread: from inside
    a connector's child workflow, ``input.workflow_id`` (= the child's
    Temporal id, ``<uuid>-process``) was being written to assets instead
    of the AE-assigned slug.  The resolver picks the topmost id.
    """
    set_execution_context(
        ExecutionContext(
            execution_type="workflow",
            workflow_id="64b1330d-4635-4d9d-99c1-1ee2c78105ea-process",
            workflow_run_id="019dd951-76a9-7125-aa42-86f0bbe36f6a",
            parent_workflow_id="dbt-AMBSvQPJ",
            parent_run_id="3d4aa53e-f47a-4d2b-b120-79db652ff759",
        )
    )
    set_correlation_context(CorrelationContext(correlation_id="corr-2"))

    details = resolve_last_sync_details()
    assert details.workflow_name == "dbt-AMBSvQPJ"
    assert details.run == "corr-2"


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
    set_execution_context(
        ExecutionContext(execution_type="workflow", workflow_id="dbt-XYZ")
    )
    set_correlation_context(CorrelationContext(correlation_id="corr-batch"))

    assets = [{"typeName": "Table"}, {"typeName": "Column"}, {"typeName": "Schema"}]
    out = set_last_sync_details_bulk(assets)

    assert len(out) == 3
    first_run_at = out[0]["attributes"][LAST_SYNC_RUN_AT]
    for asset in out:
        assert asset["attributes"][LAST_SYNC_RUN] == "corr-batch"
        assert asset["attributes"][LAST_SYNC_WORKFLOW_NAME] == "dbt-XYZ"
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
