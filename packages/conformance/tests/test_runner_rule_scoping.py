"""Tests for the runner's native ``--rule`` scoping.

The follow-up review on the rule_ids post-filter asked for this: series vs
individual rule kept coming up, so per-rule is now a first-class runner
capability rather than every consumer's post-filter. The contract under test:
one rule in, exactly that rule's findings AND exactly that rule's descriptor
out — a --rule SARIF is context-sized for a model, not fleet-sized.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conformance.suite import runner


@pytest.fixture()
def app_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "atlan-demo-app"\nversion = "0.1.0"\n'
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "import asyncio\n\n\n"
        "async def run(tasks):\n"
        "    results = await asyncio.gather(*tasks, return_exceptions=True)\n"
        "    return len(results)\n"
    )
    return tmp_path


def _run(argv: list[str]) -> int:
    return runner.main(argv)


def test_rule_scoped_run_reports_only_that_rule(app_repo: Path) -> None:
    out = app_repo / "out.sarif"
    _run(
        ["--repo", str(app_repo), "--rule", "E010", "--output", str(out), "--exit-zero"]
    )
    doc = json.loads(out.read_text())
    run = doc["runs"][0]
    assert {r["ruleId"] for r in run["results"]} == {"E010"}


def test_rule_scoped_sarif_squeezes_the_catalog(app_repo: Path) -> None:
    """The whole point: a one-rule SARIF must not ship 150+ descriptors —
    consumers feed this to models, and unused descriptors are context waste."""
    out = app_repo / "out.sarif"
    _run(
        ["--repo", str(app_repo), "--rule", "E010", "--output", str(out), "--exit-zero"]
    )
    rules = json.loads(out.read_text())["runs"][0]["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["E010"]


def test_sibling_rules_from_the_same_series_pass_are_dropped(app_repo: Path) -> None:
    """The E-series module scans in one pass; siblings it finds are outside a
    --rule run's contract and must not leak into results or the exit state.

    The planted sibling is E002 (typed ``except: pass``) — BLOCK tier — and the
    requested rule E010 is WARN, so a correctly scoped run has nothing
    gate-blocking left and must return 0 on its own, without ``--exit-zero``
    masking it.  The control run pins that premise: drop the scoping and the
    exact same repo fails the gate.
    """
    (app_repo / "app" / "extra.py").write_text(
        "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
    )
    out = app_repo / "out.sarif"
    scoped_rc = _run(["--repo", str(app_repo), "--rule", "E010", "--output", str(out)])
    doc = json.loads(out.read_text())
    assert {r["ruleId"] for r in doc["runs"][0]["results"]} == {"E010"}
    assert scoped_rc == 0

    control = app_repo / "control.sarif"
    control_rc = _run(
        ["--repo", str(app_repo), "--series", "E", "--output", str(control)]
    )
    control_ids = {
        r["ruleId"] for r in json.loads(control.read_text())["runs"][0]["results"]
    }
    assert "E002" in control_ids
    assert control_rc != 0


def test_unknown_rule_id_fails_loudly_never_silently_empty(app_repo: Path) -> None:
    """The exact bug class `--series L004` used to cause: a bad selector must
    error, not select zero checks and report a clean repo."""
    with pytest.raises(SystemExit, match="unknown rule id"):
        _run(["--repo", str(app_repo), "--rule", "Z999"])


def test_rule_and_series_are_mutually_exclusive(app_repo: Path) -> None:
    with pytest.raises(SystemExit):
        _run(["--repo", str(app_repo), "--rule", "E010", "--series", "E"])


def test_multiple_rules_across_series_derive_both_series(app_repo: Path) -> None:
    out = app_repo / "out.sarif"
    _run(
        [
            "--repo",
            str(app_repo),
            "--rule",
            "E010,L004",
            "--output",
            str(out),
            "--exit-zero",
        ]
    )
    rules = json.loads(out.read_text())["runs"][0]["tool"]["driver"]["rules"]
    assert {r["id"] for r in rules} == {"E010", "L004"}
