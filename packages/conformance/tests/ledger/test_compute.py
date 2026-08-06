"""Tests for the Integration Lane Ledger scorer.

The load-bearing behaviours here are the two anti-rot verifications: the
denominator must come from the code rather than the ledger, and cadence must
come from GitHub rather than the ledger.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest
from conformance.ledger.compute import (
    LedgerDriftError,
    evaluate,
    evaluate_connector,
    scan_entrypoints,
)
from conformance.ledger.schema import (
    Cadence,
    ConnectorLedger,
    Depth,
    Evidence,
    Lane,
    Ledger,
    Realism,
    Workflow,
)


@dataclass
class FakeRun:
    trigger: str
    conclusion: str
    age_days: int = 1


class FakeActions:
    def __init__(self, runs: dict[str, FakeRun | None]) -> None:
        self._runs = runs

    def latest_run(self, repo, workflow_file, job=None):  # noqa: ARG002
        return self._runs.get(workflow_file)


def _lane(realism="L", depth="V", cadence="A", ci_workflow=".github/workflows/ci.yaml"):
    return Lane(
        realism=Realism(realism),
        depth=Depth(depth),
        cadence=Cadence(cadence),
        evidence=Evidence(ci_workflow=ci_workflow, ci_job="test"),
    )


def _connector(name="app-x", **lane_kwargs):
    return ConnectorLedger(
        name=name,
        workflows=(
            Workflow(id="crawler", declared_at="app/x.py:1", lane=_lane(**lane_kwargs)),
        ),
    )


def _write_app(tmp_path: Path, source: str) -> Path:
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    (app / "main.py").write_text(textwrap.dedent(source))
    return tmp_path


# ---------------------------------------------------------------- scanning


def test_scan_finds_entrypoint_decorated_methods(tmp_path):
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, inp): ...

            @entrypoint(name="miner")
            async def miner(self, inp): ...

            @task
            async def fetch_tables(self, inp): ...
        """,
    )
    assert scan_entrypoints(repo) == {"crawler", "miner"}


def test_scan_falls_back_to_run_override_for_single_workflow_apps(tmp_path):
    """monte-carlo, mssql and fivetran declare their workflow this way."""
    repo = _write_app(
        tmp_path,
        """
        class MyApp(BaseMetadataExtractor):
            async def run(self, inp): ...
        """,
    )
    assert scan_entrypoints(repo) == {"run"}


def test_scan_ignores_run_when_entrypoints_exist(tmp_path):
    """databricks and powerbi have both; there run() is the SDK orchestrator."""
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, inp): ...

            async def run(self, inp): ...
        """,
    )
    assert scan_entrypoints(repo) == {"crawler"}


def test_scan_skips_unparseable_modules(tmp_path):
    repo = _write_app(tmp_path, "def broken(:\n")
    assert scan_entrypoints(repo) == set()


# ------------------------------------------------------- denominator drift


def test_unclassified_entrypoint_is_drift(tmp_path):
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, inp): ...

            @entrypoint
            async def miner(self, inp): ...
        """,
    )
    with pytest.raises(LedgerDriftError) as excinfo:
        evaluate_connector(_connector(), repo, None)
    assert excinfo.value.missing == {"miner"}


def test_ledger_row_without_code_is_drift(tmp_path):
    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def miner(self, inp): ...
        """,
    )
    with pytest.raises(LedgerDriftError) as excinfo:
        evaluate_connector(_connector(), repo, None)
    assert excinfo.value.stale == {"crawler"}


def test_explicit_exclusion_satisfies_the_denominator(tmp_path):
    from conformance.ledger.schema import Exclusion

    repo = _write_app(
        tmp_path,
        """
        class MyApp(App):
            @entrypoint
            async def crawler(self, inp): ...

            @entrypoint
            async def migrate_once(self, inp): ...
        """,
    )
    entry = ConnectorLedger(
        name="app-x",
        workflows=(Workflow(id="crawler", declared_at="", lane=_lane()),),
        excluded=(Exclusion(id="migrate_once", reason="one-time", ticket="X-1"),),
    )
    result = evaluate_connector(entry, repo, None)
    assert result.total == 1


# ------------------------------------------------------------ qualification


@pytest.mark.parametrize(
    ("realism", "depth", "expected"),
    [
        ("L", "G", True),
        ("L", "V", True),
        ("R", "G", True),
        ("L", "C", False),  # counts only
        ("L", "E", False),  # envelope only - the salesforce shape
        ("S", "V", False),  # mocked source - the powerbi shape
        ("-", "-", False),
    ],
)
def test_only_real_source_plus_validated_output_qualifies(realism, depth, expected):
    actions = FakeActions({".github/workflows/ci.yaml": FakeRun("push", "success")})
    result = evaluate_connector(_connector(realism=realism, depth=depth), None, actions)
    assert result.workflows[0].covered is expected


def test_lane_not_wired_into_ci_never_qualifies():
    result = evaluate_connector(_connector(ci_workflow=None), None, FakeActions({}))
    assert result.workflows[0].covered is False
    assert "not wired" in result.workflows[0].reason


# ------------------------------------------------- cadence is not trusted


def test_declared_cadence_does_not_earn_credit_when_gh_says_manual():
    """Writing `cadence: A` in the YAML must not beat what GitHub reports."""
    actions = FakeActions(
        {".github/workflows/ci.yaml": FakeRun("workflow_dispatch", "success")}
    )
    result = evaluate_connector(_connector(cadence="A"), None, actions)
    assert result.workflows[0].covered is False
    assert "workflow_dispatch" in result.workflows[0].reason


def test_red_automatic_run_does_not_qualify():
    actions = FakeActions({".github/workflows/ci.yaml": FakeRun("push", "failure")})
    result = evaluate_connector(_connector(), None, actions)
    assert result.workflows[0].covered is False


def test_schedule_counts_as_automatic():
    actions = FakeActions({".github/workflows/ci.yaml": FakeRun("schedule", "success")})
    result = evaluate_connector(_connector(), None, actions)
    assert result.workflows[0].covered is True


def test_missing_run_history_does_not_qualify():
    result = evaluate_connector(_connector(), None, FakeActions({}))
    assert result.workflows[0].covered is False


def test_offline_mode_flags_cadence_as_unverified():
    result = evaluate_connector(_connector(cadence="A"), None, None)
    assert result.workflows[0].covered is True
    assert "not verified" in result.workflows[0].reason


# ------------------------------------------------------------------ fleet


def test_fleet_irr_is_workflow_weighted_not_a_mean_of_ratios():
    """A 1-workflow app at 100% must not outweigh a 3-workflow app at 0%."""
    good = ConnectorLedger(
        name="small",
        workflows=(Workflow(id="run", declared_at="", lane=_lane()),),
    )
    bad = ConnectorLedger(
        name="big",
        workflows=tuple(
            Workflow(id=f"w{i}", declared_at="", lane=_lane(depth="E"))
            for i in range(3)
        ),
    )
    ledger = Ledger(version=1, connectors={"small": good, "big": bad})
    actions = FakeActions({".github/workflows/ci.yaml": FakeRun("push", "success")})
    fleet = evaluate(ledger, repo_root=None, actions=actions)
    assert (fleet.covered, fleet.total) == (1, 4)
    assert fleet.irr == pytest.approx(0.25)
