"""Tests for the skip-tolerant DAG success gate (BaseE2ETest._core_dag_ok).

Pure logic — no tenant needed; we build fake DAGRunResults and assert the gate
tolerates intentionally-skipped downstream nodes when lineage isn't expected,
while staying strict (== all_nodes_succeeded) when it is.
"""

from __future__ import annotations

from application_sdk.testing.e2e import BaseE2ETest
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)


def _node(name: str, status: DAGNodeStatus, error: str | None = None) -> DAGNodeResult:
    return DAGNodeResult(
        name=name,
        status=status,
        started_at_ms=None,
        completed_at_ms=None,
        error_message=error,
    )


def _result(*nodes: DAGNodeResult) -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.SUCCEEDED,
        nodes=list(nodes),
    )


class _LineageExpected(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expect_lineage = True


class _NoLineage(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expect_lineage = False


class _NoLineageWithBranch(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expect_lineage = False
    required_dag_nodes = ("branch", "extract", "publish")


class TestDAGNodeStatusSkipped:
    def test_skipped_and_omitted_are_terminal(self) -> None:
        assert DAGNodeStatus.SKIPPED.is_terminal
        assert DAGNodeStatus.OMITTED.is_terminal

    def test_skipped_is_not_success(self) -> None:
        assert not DAGNodeStatus.SKIPPED.is_success
        assert not DAGNodeStatus.OMITTED.is_success

    def test_is_skipped_flag(self) -> None:
        assert DAGNodeStatus.SKIPPED.is_skipped
        assert DAGNodeStatus.OMITTED.is_skipped
        assert not DAGNodeStatus.SUCCEEDED.is_skipped
        assert not DAGNodeStatus.PENDING.is_skipped

    def test_skipped_parses_from_wire(self) -> None:
        from application_sdk.testing.e2e.client import _safe_node_status

        assert _safe_node_status("Skipped") is DAGNodeStatus.SKIPPED
        assert _safe_node_status("Omitted") is DAGNodeStatus.OMITTED


class TestCoreDAGOkLineageExpected:
    def test_all_succeeded_passes(self) -> None:
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
        )
        assert _LineageExpected()._core_dag_ok(r) is True

    def test_skipped_node_fails_when_lineage_expected(self) -> None:
        # Strict gate: a skipped lineage node is a failure when lineage IS wanted.
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.SKIPPED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
        )
        assert _LineageExpected()._core_dag_ok(r) is False


class TestCoreDAGOkNoLineage:
    def test_skipped_downstream_tolerated(self) -> None:
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.SKIPPED),
            _node("lineage-app", DAGNodeStatus.SKIPPED),
            _node("lineage-publish", DAGNodeStatus.OMITTED),
        )
        assert _NoLineage()._core_dag_ok(r) is True

    def test_pending_downstream_tolerated(self) -> None:
        # Older AE service downgrades Skipped -> Pending; still tolerated.
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.PENDING),
        )
        assert _NoLineage()._core_dag_ok(r) is True

    def test_required_node_not_succeeded_fails(self) -> None:
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.PENDING),
        )
        assert _NoLineage()._core_dag_ok(r) is False

    def test_missing_required_node_fails(self) -> None:
        r = _result(_node("extract", DAGNodeStatus.SUCCEEDED))
        assert _NoLineage()._core_dag_ok(r) is False

    def test_hard_failure_anywhere_fails(self) -> None:
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.FAILED),
        )
        assert _NoLineage()._core_dag_ok(r) is False

    def test_error_message_on_skipped_node_fails(self) -> None:
        # A node carrying an error message is a genuine failure even if its
        # status string isn't in the hard-fail set.
        r = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.SKIPPED, error="boom"),
        )
        assert _NoLineage()._core_dag_ok(r) is False

    def test_branch_node_required_when_declared(self) -> None:
        ok = _result(
            _node("branch", DAGNodeStatus.SUCCEEDED),
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
            _node("qi", DAGNodeStatus.SKIPPED),
        )
        assert _NoLineageWithBranch()._core_dag_ok(ok) is True

        missing_branch = _result(
            _node("extract", DAGNodeStatus.SUCCEEDED),
            _node("publish", DAGNodeStatus.SUCCEEDED),
        )
        assert _NoLineageWithBranch()._core_dag_ok(missing_branch) is False
