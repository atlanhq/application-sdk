"""Tests for BaseE2ETest._build_certification_report (per-combo cert record).

Pure logic — no tenant / Atlas needed. We build a fake FullDAGOutcome and call
the report builder directly, asserting the certified verdict and the three
certification dimensions (workflow status, asset presence, count parity).
"""

from __future__ import annotations

from application_sdk.testing.e2e import BaseE2ETest
from application_sdk.testing.e2e.base import FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)


class _Conn(BaseE2ETest):
    connector_short_name = "mysql"
    argo_package_name = "@atlan/mysql"
    argo_template_name = "t"
    expected_min_asset_counts = {"Table": 5}
    expect_lineage = False


def _node(name: str, ok: bool) -> DAGNodeResult:
    return DAGNodeResult(
        name=name,
        status=DAGNodeStatus.SUCCEEDED if ok else DAGNodeStatus.FAILED,
        started_at_ms=0,
        completed_at_ms=1000,
        error_message=None if ok else "boom",
    )


def _outcome(*, nodes_ok: bool, conn: bool, counts: dict[str, int]) -> FullDAGOutcome:
    return FullDAGOutcome(
        ae_result=DAGRunResult(
            run_id="r1",
            workflow_slug="mysql-abc",
            status=DAGRunStatus.SUCCEEDED if nodes_ok else DAGRunStatus.FAILED,
            nodes=[_node("extract", nodes_ok)],
        ),
        connection_qualified_name="default/mysql/123",
        connection_in_atlas=conn,
        asset_counts=counts,
        total_assets=sum(counts.values()),
    )


def test_all_green_is_certified() -> None:
    outcome = _outcome(nodes_ok=True, conn=True, counts={"Table": 7})
    report = _Conn()._build_certification_report(outcome, asset_failures=[])
    assert report["certified"] is True
    assert report["checks"]["workflow_nodes_succeeded"] is True
    assert report["checks"]["connection_in_atlas"] is True
    assert report["checks"]["asset_expectations_met"] is True
    # expect_lineage False → lineage is not asserted for this connector.
    assert report["checks"]["lineage_present"] is None


def test_asset_shortfall_is_not_certified() -> None:
    outcome = _outcome(nodes_ok=True, conn=True, counts={"Table": 2})
    report = _Conn()._build_certification_report(
        outcome, asset_failures=["  - Table: got 2, expected >= 5"]
    )
    assert report["certified"] is False
    assert report["checks"]["asset_expectations_met"] is False
    assert report["asset_failures"]


def test_failed_nodes_is_not_certified() -> None:
    outcome = _outcome(nodes_ok=False, conn=False, counts={})
    report = _Conn()._build_certification_report(outcome, asset_failures=[])
    assert report["certified"] is False
    assert report["checks"]["workflow_nodes_succeeded"] is False
    assert report["workflow"]["failed_nodes"] == ["extract"]


def test_combo_label_from_env(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_PROFILE", "aws/s3")
    outcome = _outcome(nodes_ok=True, conn=True, counts={"Table": 7})
    report = _Conn()._build_certification_report(outcome, asset_failures=[])
    assert report["combo"] == "aws/s3"
