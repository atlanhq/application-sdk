"""``expected_lineage_edges`` on ``BaseE2ETest``, end to end.

The failure this knob exists for: a lineage change that lands asset counts
identical to a good run and still produces no field-level links. Counts cannot
see processes, and ``expect_lineage`` passes as soon as *any* lineage exists, so
the only way to fail that run is to name the link that should have landed.

Three claims, in the order a run makes them: the declaration is validated and
resolved like every other per-run knob; a declared edge below its floor fails
the leg naming the edge, the floor and the observed count; and a read that
could not be taken is ungraded — never a pass, never a zero.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from application_sdk.testing.e2e._errors import (
    AtlasReadIndeterminateError,
    InvalidLineageEdgeDeclarationError,
)
from application_sdk.testing.e2e.base import BaseE2ETest, DAGSpec, FullDAGOutcome
from application_sdk.testing.e2e.client import (
    DAGNodeResult,
    DAGNodeStatus,
    DAGRunResult,
    DAGRunStatus,
)
from application_sdk.testing.harness.expectations import (
    LineageEdgeCounts,
    Unreadable,
)
from application_sdk.testing.harness.outcome import Indeterminate, Settled

_QN = "default/sigma/1700000000123456"
_EDGE = ("SigmaDataElementField", "SigmaDataModelColumn")
_EDGE_KEY = "SigmaDataElementField -> SigmaDataModelColumn"


class _Sigma(BaseE2ETest):
    connector_short_name = "sigma"
    argo_package_name = "@atlan/sigma"
    argo_template_name = "atlan-sigma"
    expected_min_asset_counts = {"SigmaDataElementField": 1}
    expected_lineage_edges = {_EDGE_KEY: 2}


class _NoEdges(BaseE2ETest):
    connector_short_name = "sigma"
    argo_package_name = "@atlan/sigma"
    argo_template_name = "atlan-sigma"
    expected_min_asset_counts = {"SigmaDataElementField": 1}


def _suite(cls: type[BaseE2ETest] = _Sigma) -> BaseE2ETest:
    suite = cls()
    # setup_method (which mints this) isn't run in pure unit tests.
    suite.connection_qualified_name = _QN
    return suite


def _succeeded() -> DAGRunResult:
    return DAGRunResult(
        run_id="r",
        workflow_slug="s",
        status=DAGRunStatus.SUCCEEDED,
        nodes=[
            DAGNodeResult(
                name="extract",
                status=DAGNodeStatus.SUCCEEDED,
                started_at_ms=None,
                completed_at_ms=None,
                error_message=None,
            )
        ],
    )


def _outcome(edge_read: Any) -> FullDAGOutcome:
    """A run that passed every other assertion, so only the edges are on trial."""
    return FullDAGOutcome(
        ae_result=_succeeded(),
        connection_qualified_name=_QN,
        connection_in_atlas=True,
        asset_counts={"SigmaDataElementField": 5},
        asset_count_reads={"SigmaDataElementField": 5},
        total_asset_read=5,
        lineage_present=True,
        lineage_read=True,
        lineage_edge_reads=edge_read,
    )


def _settled(value: Any) -> Any:
    """A reader stub answering ``Settled(value)`` whatever it is asked."""

    async def _reader(*_args: Any, **_kwargs: Any) -> Any:
        return Settled(label="stub", attempts=1, elapsed=timedelta(0), value=value)

    return _reader


# ---------------------------------------------------------------------------
# Declaration and resolution
# ---------------------------------------------------------------------------


class TestResolution:
    def test_the_class_declaration_resolves_under_its_canonical_key(self) -> None:
        assert _suite()._dag.expected_lineage_edges == {_EDGE_KEY: 2}

    def test_a_dag_spec_overrides_the_class_declaration(self) -> None:
        resolved = _suite().resolve_dag(
            DAGSpec(expected_lineage_edges={"Column->SigmaDataElementField": 1})
        )
        assert resolved.expected_lineage_edges == {"Column -> SigmaDataElementField": 1}

    def test_a_dag_spec_that_says_nothing_inherits_the_class(self) -> None:
        resolved = _suite().resolve_dag(DAGSpec())
        assert resolved.expected_lineage_edges == {_EDGE_KEY: 2}

    @pytest.mark.parametrize(
        "key",
        [
            "SigmaDataElementField",
            "SigmaDataElementField -> ",
            "A -> B -> C",
            "Sigma Field -> Column",
            "1Column -> Table",
            "Column => Table",
        ],
    )
    def test_a_malformed_key_is_a_configuration_error(self, key: str) -> None:
        with pytest.raises(InvalidLineageEdgeDeclarationError) as exc:
            _suite().resolve_dag(DAGSpec(expected_lineage_edges={key: 1}))
        assert repr(key) in str(exc.value)
        assert "SourceType -> TargetType" in str(exc.value)

    @pytest.mark.parametrize("floor", [0, -1])
    def test_a_floor_below_one_is_rejected(self, floor: int) -> None:
        with pytest.raises(InvalidLineageEdgeDeclarationError) as exc:
            _suite().resolve_dag(DAGSpec(expected_lineage_edges={_EDGE_KEY: floor}))
        assert ">= 1" in str(exc.value)

    def test_the_same_edge_spelled_twice_is_rejected(self) -> None:
        with pytest.raises(InvalidLineageEdgeDeclarationError):
            _suite().resolve_dag(
                DAGSpec(
                    expected_lineage_edges={
                        _EDGE_KEY: 1,
                        "SigmaDataElementField->SigmaDataModelColumn": 1,
                    }
                )
            )

    def test_edges_resolve_on_a_run_that_does_not_expect_lineage(self) -> None:
        # expect_lineage only relaxes the DAG gate and the presence check; the
        # run is submitted the same, and a BI connector's transform publishes
        # its processes with the lineage nodes skipped.
        resolved = _suite().resolve_dag(DAGSpec(expect_lineage=False))
        assert resolved.expected_lineage_edges == {_EDGE_KEY: 2}


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


class TestGrading:
    def test_an_edge_at_its_floor_passes(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 2}, scanned=10)
        _suite()._assert_full_dag_outcome(_outcome(read))  # must not raise

    def test_an_edge_observed_zero_times_fails_naming_edge_floor_and_count(
        self,
    ) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 0}, scanned=10)
        with pytest.raises(AssertionError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(read))
        message = str(exc.value)
        assert _EDGE_KEY in message
        assert "observed 0" in message
        assert "expected >= 2" in message
        assert _QN in message

    def test_an_edge_below_its_floor_fails(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 1}, scanned=10)
        with pytest.raises(AssertionError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(read))
        assert "observed 1" in str(exc.value)

    def test_a_missing_read_fails_rather_than_passing(self) -> None:
        with pytest.raises(AssertionError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(None))
        assert _EDGE_KEY in str(exc.value)

    def test_an_unreadable_read_is_ungraded_not_a_failure_and_not_zero(
        self,
    ) -> None:
        read = Unreadable(cause=RuntimeError("atlas is down"))
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(read))
        assert not isinstance(exc.value, AssertionError)
        assert exc.value.checks is not None and _EDGE_KEY in exc.value.checks
        assert "observed 0" not in str(exc.value)

    def test_a_truncated_scan_below_the_floor_is_ungraded(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 1}, scanned=5000, truncated=True)
        with pytest.raises(AtlasReadIndeterminateError) as exc:
            _suite()._assert_full_dag_outcome(_outcome(read))
        assert not isinstance(exc.value, AssertionError)

    def test_a_truncated_scan_that_already_met_the_floor_passes(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 2}, scanned=5000, truncated=True)
        _suite()._assert_full_dag_outcome(_outcome(read))  # must not raise

    def test_a_suite_that_declares_nothing_grades_nothing(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 0})
        _suite(_NoEdges)._assert_full_dag_outcome(_outcome(read))  # must not raise


class TestFixtureSuites:
    """Suites that crawl once in a fixture and assert piecemeal never reach
    ``_assert_full_dag_outcome``, so the edges need their own entry point."""

    def test_an_unmet_edge_is_reported_to_a_fixture_suite(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 1}, scanned=10)
        [line] = _suite()._evaluate_lineage_edges(_outcome(read).lineage_edge_reads)
        assert _EDGE_KEY in line and "observed 1" in line and "expected >= 2" in line

    def test_a_met_edge_reports_nothing(self) -> None:
        read = LineageEdgeCounts(counts={_EDGE: 2}, scanned=10)
        assert _suite()._evaluate_lineage_edges(read) == []

    def test_an_unreadable_read_raises_rather_than_reporting_zero(self) -> None:
        read = Unreadable(cause=RuntimeError("atlas is down"))
        with pytest.raises(AtlasReadIndeterminateError):
            _suite()._evaluate_lineage_edges(read)


# ---------------------------------------------------------------------------
# The read itself
# ---------------------------------------------------------------------------


class TestTheRead:
    async def test_the_reader_is_called_with_the_declared_pairs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.testing.e2e import base as base_module

        calls: list[tuple[str, list[tuple[str, str]]]] = []

        async def _fake_edges(_client: Any, qualified_name: str, pairs: Any) -> Any:
            calls.append((qualified_name, list(pairs)))
            return Settled(
                label="edges",
                attempts=1,
                elapsed=timedelta(0),
                value=LineageEdgeCounts(counts={_EDGE: 3}, scanned=3),
            )

        monkeypatch.setattr(base_module.atlas, "count_lineage_edges", _fake_edges)
        monkeypatch.setattr(
            base_module.atlas,
            "count_assets",
            _settled({"SigmaDataElementField": 5}),
        )
        monkeypatch.setattr(base_module.atlas, "count_total_assets", _settled(5))
        monkeypatch.setattr(
            base_module.atlas,
            "count_lineage",
            _settled({"SigmaDataElementField": 5}),
        )

        outcome = await _suite()._read_inventory(object(), _succeeded())

        assert calls == [(_QN, [_EDGE])]
        assert outcome.lineage_edge_counts == {_EDGE_KEY: 3}
        assert isinstance(outcome.lineage_edge_reads, LineageEdgeCounts)

    async def test_an_unreadable_read_has_no_settled_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.testing.e2e import base as base_module

        async def _failed(*_args: Any, **_kwargs: Any) -> Any:
            return Indeterminate(
                label="edges",
                attempts=1,
                elapsed=timedelta(0),
                cause=RuntimeError("atlas is down"),
                transient_failures=1,
            )

        monkeypatch.setattr(base_module.atlas, "count_lineage_edges", _failed)
        monkeypatch.setattr(
            base_module.atlas,
            "count_assets",
            _settled({"SigmaDataElementField": 5}),
        )
        monkeypatch.setattr(base_module.atlas, "count_total_assets", _settled(5))
        monkeypatch.setattr(
            base_module.atlas,
            "count_lineage",
            _settled({"SigmaDataElementField": 5}),
        )

        outcome = await _suite()._read_inventory(object(), _succeeded())

        assert isinstance(outcome.lineage_edge_reads, Unreadable)
        assert outcome.lineage_edge_counts == {}

    async def test_a_suite_that_declares_nothing_makes_no_edge_search(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk.testing.e2e import base as base_module

        called = False

        async def _never(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal called
            called = True
            raise AssertionError("the lineage-edge reader must not be called")

        monkeypatch.setattr(base_module.atlas, "count_lineage_edges", _never)
        monkeypatch.setattr(
            base_module.atlas,
            "count_assets",
            _settled({"SigmaDataElementField": 1}),
        )
        monkeypatch.setattr(base_module.atlas, "count_total_assets", _settled(1))
        monkeypatch.setattr(
            base_module.atlas,
            "count_lineage",
            _settled({"SigmaDataElementField": 1}),
        )

        outcome = await _suite(_NoEdges)._read_inventory(object(), _succeeded())
        assert not called
        assert outcome.lineage_edge_reads is None
