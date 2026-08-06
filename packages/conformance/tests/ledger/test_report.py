"""Tests for scoring a connector's integration coverage."""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path

from conformance.ledger.report import evaluate_repo
from conformance.ledger.schema import Depth

_CI = ".github/workflows/tests.yaml"


@dataclass
class FakeRun:
    trigger: str
    conclusion: str
    age_days: int = 1


class FakeActions:
    def __init__(self, run: FakeRun | None) -> None:
        self._run = run

    def latest_run(self, repo, workflow_file, job=None):  # noqa: ARG002
        return self._run


def _repo(tmp_path: Path, *, app: str, tests: str = "", manifest: dict | None = None):
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text(textwrap.dedent(app))
    if tests:
        t = tmp_path / "tests" / "integration" / "test_x.py"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(textwrap.dedent(tests))
    if manifest is not None:
        m = tmp_path / "app" / "generated" / "manifest.json"
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(json.dumps({"dag": manifest}))
    return tmp_path


_TWO_ENTRYPOINTS = """
    class MyApp(App):
        @entrypoint
        async def crawler(self, i): ...

        @entrypoint
        async def miner(self, i): ...
"""


def test_undeclared_workflows_score_zero(tmp_path):
    """The honest default: nothing declared means nothing verifiable."""
    repo = _repo(tmp_path, app=_TWO_ENTRYPOINTS)
    report = evaluate_repo(repo, "atlan-x-app")
    assert (report.covered, report.total) == (0, 2)
    assert all("no integration lane declares it" in w.reason for w in report.workflows)


def test_declared_and_validated_workflow_counts(tmp_path):
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests="""
        Scenario(name="c", api="workflow", assert_that={}, entrypoint="crawler",
                 schema_base_path="tests/schema")
        """,
    )
    report = evaluate_repo(
        repo, "atlan-x-app", FakeActions(FakeRun("push", "success")), _CI
    )
    verdicts = {w.id: w for w in report.workflows}
    assert verdicts["crawler"].covered is True
    assert verdicts["crawler"].depth is Depth.VALIDATED
    assert verdicts["miner"].covered is False
    assert report.covered == 1


def test_declared_without_validation_does_not_count(tmp_path):
    """Starting a workflow is not the same as checking what it produced."""
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests='Scenario(name="c", api="workflow", assert_that={}, entrypoint="crawler")\n',
    )
    report = evaluate_repo(
        repo, "atlan-x-app", FakeActions(FakeRun("push", "success")), _CI
    )
    crawler = next(w for w in report.workflows if w.id == "crawler")
    assert crawler.covered is False
    assert crawler.depth is Depth.COUNTS
    assert "no output validation" in crawler.reason


def test_golden_declaration_counts(tmp_path):
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests="""
        Scenario(name="c", api="workflow", assert_that={}, entrypoint="crawler",
                 expected_data="tests/golden/crawler.json")
        """,
    )
    report = evaluate_repo(
        repo, "atlan-x-app", FakeActions(FakeRun("push", "success")), _CI
    )
    crawler = next(w for w in report.workflows if w.id == "crawler")
    assert crawler.covered is True
    assert crawler.depth is Depth.GOLDEN


def test_manual_trigger_does_not_count(tmp_path):
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests="""
        Scenario(name="c", api="workflow", assert_that={}, entrypoint="crawler",
                 schema_base_path="s")
        """,
    )
    report = evaluate_repo(
        repo, "atlan-x-app", FakeActions(FakeRun("workflow_dispatch", "success")), _CI
    )
    crawler = next(w for w in report.workflows if w.id == "crawler")
    assert crawler.covered is False
    assert "workflow_dispatch" in crawler.reason


def test_red_run_does_not_count(tmp_path):
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests="""
        Scenario(name="c", api="workflow", assert_that={}, entrypoint="crawler",
                 schema_base_path="s")
        """,
    )
    report = evaluate_repo(
        repo, "atlan-x-app", FakeActions(FakeRun("push", "failure")), _CI
    )
    assert report.covered == 0


def test_orphan_declaration_is_surfaced(tmp_path):
    """A suite naming a workflow the app no longer has is a real defect."""
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests="""
        Scenario(name="c", api="workflow", assert_that={}, entrypoint="renamed_away",
                 schema_base_path="s")
        """,
    )
    report = evaluate_repo(repo, "atlan-x-app")
    assert report.orphan_declarations == ["renamed_away"]


def test_denominator_tracks_the_code_not_a_stored_list(tmp_path):
    """Adding an entrypoint changes the score with no file to update."""
    repo = _repo(tmp_path, app=_TWO_ENTRYPOINTS)
    assert evaluate_repo(repo, "atlan-x-app").total == 2

    (repo / "app" / "main.py").write_text(
        textwrap.dedent(
            _TWO_ENTRYPOINTS
            + """
        @entrypoint
        async def clean(self, i): ...
    """
        )
    )
    assert evaluate_repo(repo, "atlan-x-app").total == 3


def test_workflow_absent_from_the_manifest_is_flagged(tmp_path):
    repo = _repo(
        tmp_path,
        app=_TWO_ENTRYPOINTS,
        tests="""
        Scenario(name="c", api="workflow", assert_that={}, entrypoint="crawler",
                 schema_base_path="s")
        """,
        manifest={"extract": {"inputs": {"workflow_type": "x:miner"}}},
    )
    report = evaluate_repo(
        repo, "atlan-x-app", FakeActions(FakeRun("push", "success")), _CI
    )
    crawler = next(w for w in report.workflows if w.id == "crawler")
    assert "not present in any generated manifest DAG" in crawler.reason


def test_repo_without_an_app_package_scores_nothing(tmp_path):
    report = evaluate_repo(tmp_path, "atlan-x-app")
    assert (report.covered, report.total, report.irr) == (0, 0, 0.0)
