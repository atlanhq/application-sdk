"""Unit tests for the last-sync details primitive (BLDX-1229).

Covers the four interesting paths through ``resolve_last_sync_details`` —
top-level workflow, child workflow (where the AE-assigned id is exposed via
``parent_workflow_id``), no execution context (tests / CLI), and explicit
overrides — plus the Asset-based stamping helpers (the single recommended
API surface; earlier dict-shaped helpers were removed in BLDX-1229).
"""

from __future__ import annotations

from datetime import UTC

import pytest

from application_sdk.observability import (
    CorrelationContext,
    ExecutionContext,
    set_correlation_context,
    set_execution_context,
)
from application_sdk.transformers.common.last_sync import (
    LastSyncDetails,
    resolve_last_sync_details,
    set_last_sync_details_on_asset,
    set_last_sync_details_on_assets_bulk,
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
# set_last_sync_details_on_asset (pyatlan Asset — the only stamping path)
# ---------------------------------------------------------------------------


def _epoch_ms_to_datetime(epoch_ms: int):
    """pyatlan stores ``last_sync_run_at`` internally as a ``datetime``.

    pydantic v2 (which pyatlan uses) auto-detects seconds vs milliseconds
    by magnitude: ``|value| <= 2e10`` → seconds, else milliseconds.  All
    fixtures below use realistic recent-epoch values (>2e10) so the unit
    is unambiguous.
    """
    from datetime import datetime as _dt

    return _dt.fromtimestamp(epoch_ms / 1000, tz=UTC)


# Use values > 2e10 so pydantic parses as ms (not seconds).
_TEST_RUN_AT_MS = 1700000000000  # 2023-11-14T22:13:20Z
_TEST_RUN_AT_MS_ALT = 1700000123000
_TEST_RUN_AT_MS_BULK = 1700000456000


def test_set_on_asset_stamps_typed_attributes():
    """Direct attribute assignment on a pyatlan Asset subclass.

    Uses ``Database`` (any subclass works — covariance via the base
    ``Asset`` type).  pyatlan accepts an int (epoch ms) for
    ``last_sync_run_at`` and stores it as a ``datetime`` internally.
    """
    from pyatlan.model.assets import Database

    asset = Database()
    out = set_last_sync_details_on_asset(
        asset,
        run="r",
        workflow_name="w",
        run_at_ms=_TEST_RUN_AT_MS,
    )
    assert out is asset  # mutates in place
    assert asset.last_sync_run == "r"
    assert asset.last_sync_workflow_name == "w"
    assert asset.last_sync_run_at == _epoch_ms_to_datetime(_TEST_RUN_AT_MS)


def test_set_on_asset_resolves_from_context_when_no_overrides():
    from pyatlan.model.assets import Table

    ae_wf_id = "4b9eade4-de53-4b69-9010-2446e0a8f85c"
    corr_id = "d637c39c-81a0-48b5-bf36-312108e4615c"
    set_execution_context(
        ExecutionContext(execution_type="workflow", workflow_id=ae_wf_id)
    )
    set_correlation_context(CorrelationContext(correlation_id=corr_id))

    asset = Table()
    set_last_sync_details_on_asset(asset)

    assert asset.last_sync_run == corr_id
    assert asset.last_sync_workflow_name == ae_wf_id
    assert asset.last_sync_run_at is not None


def test_set_on_asset_skips_empty_run_and_workflow_name_but_always_writes_run_at():
    """Empty resolutions don't get written — operator dashboards filter on
    presence, so writing ``""`` would be worse than omitting.  ``run_at`` is
    always populated (clock is always available) so it's always written."""
    from pyatlan.model.assets import Database

    asset = Database()
    set_last_sync_details_on_asset(
        asset, run="", workflow_name="", run_at_ms=_TEST_RUN_AT_MS
    )

    assert asset.last_sync_run is None
    assert asset.last_sync_workflow_name is None
    assert asset.last_sync_run_at == _epoch_ms_to_datetime(_TEST_RUN_AT_MS)


def test_set_on_asset_details_param_wins_over_individual_overrides():
    from pyatlan.model.assets import Column

    asset = Column()
    details = LastSyncDetails(
        run="from-details", workflow_name="wf", run_at_ms=_TEST_RUN_AT_MS_ALT
    )
    set_last_sync_details_on_asset(
        asset,
        details=details,
        run="ignored",
        workflow_name="ignored",
        run_at_ms=_TEST_RUN_AT_MS,
    )
    assert asset.last_sync_run == "from-details"
    assert asset.last_sync_workflow_name == "wf"
    assert asset.last_sync_run_at == _epoch_ms_to_datetime(_TEST_RUN_AT_MS_ALT)


# ---------------------------------------------------------------------------
# set_last_sync_details_on_assets_bulk
# ---------------------------------------------------------------------------


def test_bulk_on_assets_applies_same_values_to_every_asset():
    from pyatlan.model.assets import Column, Schema, Table

    ae_wf_id = "4b9eade4-de53-4b69-9010-2446e0a8f85c"
    corr_id = "d637c39c-81a0-48b5-bf36-312108e4615c"
    set_execution_context(
        ExecutionContext(execution_type="workflow", workflow_id=ae_wf_id)
    )
    set_correlation_context(CorrelationContext(correlation_id=corr_id))

    assets = [Table(), Column(), Schema()]
    out = set_last_sync_details_on_assets_bulk(assets)

    assert len(out) == 3
    first_run_at = out[0].last_sync_run_at
    for asset in out:
        assert asset.last_sync_run == corr_id
        assert asset.last_sync_workflow_name == ae_wf_id
        # Same timestamp across the whole batch — proves the resolver
        # didn't fire per asset.
        assert asset.last_sync_run_at == first_run_at


def test_bulk_on_assets_accepts_explicit_overrides():
    from pyatlan.model.assets import Database

    out = set_last_sync_details_on_assets_bulk(
        [Database() for _ in range(3)],
        run="explicit",
        workflow_name="explicit-wf",
        run_at_ms=_TEST_RUN_AT_MS_BULK,
    )
    for asset in out:
        assert asset.last_sync_run == "explicit"
        assert asset.last_sync_workflow_name == "explicit-wf"
        assert asset.last_sync_run_at == _epoch_ms_to_datetime(_TEST_RUN_AT_MS_BULK)
