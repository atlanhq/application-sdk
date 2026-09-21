"""F019 promises executed scenarios clear a value-level gap. Prove it does.

Two halves. First, that the checkers label each F019 correctly: a *value-level*
gap (the handler was analysed, one expression's value was not resolved) names
F016 in ``cleared_by``, a *structural* gap (the analysis never reached the code)
names nothing. Second, that the runner acts on the label — a ``--with-tests``
run whose F016 matrix came back complete drops the first kind and keeps the
second, and a matrix that is anything less than complete drops neither.
"""

import json

import pytest
from conformance.suite.checks.preflight._common import build_registry, coverage_findings
from conformance.suite.checks.preflight._contracts import scan as scan_contracts
from conformance.suite.checks.preflight._untyped_failure import scan as scan_untyped
from conformance.suite.runner import main

IMPORTS = (
    "from application_sdk.handler import Handler\n"
    "from application_sdk.handler.contracts import PreflightInput, PreflightOutput, PreflightCheck\n"
    "from application_sdk.errors import AppError, AuthError\n"
)

HEAD = (
    "class H(Handler):\n"
    " async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
)


def _registry(tmp_path, source, name="handler.py"):
    path = tmp_path / name
    path.write_text(IMPORTS + source)
    return build_registry([path], tmp_path)


def _f019(findings):
    return [f for f in findings if f.rule_id == "F019"]


# --- the checkers label the two kinds apart ---------------------------------


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            HEAD + "  rows = await gather()\n  return PreflightOutput(checks=rows)\n",
            id="computed-aggregation",
        ),
        pytest.param(
            HEAD + "  rows = await gather()\n"
            "  return PreflightOutput(checks=[*rows])\n",
            id="opaque-row-in-a-list-display",
        ),
        pytest.param(
            HEAD + "  fields = build()\n"
            "  return PreflightOutput(checks=[PreflightCheck(passed=False, error=AuthError(**fields).to_failure_details())])\n",
            id="expanded-failure-constructor",
        ),
        pytest.param(
            HEAD
            + '  return PreflightOutput(checks=[PreflightCheck(passed=False, error=AuthError(message="No", suggested_action=advice()).to_failure_details())])\n',
            id="computed-suggested-action",
        ),
    ],
)
def test_value_level_gap_names_the_scenario_rule(tmp_path, source):
    findings = _f019(scan_contracts(_registry(tmp_path, source)))
    assert findings, "expected a value-level F019"
    assert all(f.cleared_by == frozenset({"F016"}) for f in findings)


def test_dynamic_passed_without_typed_error_names_the_scenario_rule(tmp_path):
    reg = _registry(
        tmp_path,
        "def check(ok):\n"
        ' return PreflightCheck(name="probe", passed=ok)\n' + HEAD + "  return None\n",
    )
    findings = _f019(scan_untyped(reg))
    assert findings, "expected a dynamic-passed F019"
    assert all(f.cleared_by == frozenset({"F016"}) for f in findings)


def test_definite_failure_is_f003_and_never_scenario_cleared(tmp_path):
    reg = _registry(
        tmp_path,
        "def check():\n"
        ' return PreflightCheck(name="probe", passed=False)\n'
        + HEAD
        + "  return None\n",
    )
    findings = scan_untyped(reg)
    assert [f.rule_id for f in findings] == ["F003"]
    assert findings[0].cleared_by == frozenset()


def test_unparsed_source_is_never_scenario_cleared(tmp_path):
    broken = tmp_path / "handler.py"
    broken.write_text("class H(:\n")
    findings = _f019(coverage_findings(build_registry([broken], tmp_path)))
    assert findings, "expected a parse-failure F019"
    assert all(f.cleared_by == frozenset() for f in findings)


def test_dynamic_callback_binding_is_never_scenario_cleared(tmp_path):
    reg = _registry(
        tmp_path,
        "async def _probe(input: PreflightInput) -> PreflightOutput:\n"
        "  return PreflightOutput(checks=[])\n"
        "preflight_check = lookup()\n",
    )
    findings = _f019(coverage_findings(reg))
    assert findings, "expected a dynamic-binding F019"
    assert all(f.cleared_by == frozenset() for f in findings)
    # It used to say "register behavioral scenarios", which clears nothing here.
    assert all("register behavioral scenarios" not in f.message for f in findings)
    assert all(
        "do not clear this" in f.message for f in findings
    ), "a finding no scenario can clear must say so rather than imply otherwise"


# --- the runner acts on the label -------------------------------------------


@pytest.fixture
def repo(tmp_path):
    """A repo with one value-level F019 and one structural F019."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-connector"\nversion="1.0.0"\n'
    )
    (tmp_path / "handler.py").write_text(
        IMPORTS + 'def check(ok):\n return PreflightCheck(name="probe", passed=ok)\n'
    )
    (tmp_path / "unparsed.py").write_text("class H(:\n")
    return tmp_path


def _run(repo, monkeypatch, summary):
    from conformance.suite.checks.preflight import _behavior

    monkeypatch.setattr(
        _behavior,
        "run_behavior",
        lambda *a, **k: _behavior.BehaviorResult([], dict(summary)),
    )
    output = repo / "report.sarif"
    main(
        [
            "--repo",
            str(repo),
            "--rule",
            "F019",
            "--with-tests",
            "--output",
            str(output),
            "--exit-zero",
        ]
    )
    return json.loads(output.read_text())["runs"][0]


COMPLETE = {"F016": {"execution": "completed", "passed": 13, "complete": True}}
INCOMPLETE = {
    "F016": {
        "execution": "completed",
        "passed": 12,
        "missing": ["x"],
        "complete": False,
    }
}


def test_complete_matrix_clears_only_the_value_level_gap(repo, monkeypatch, capsys):
    run = _run(repo, monkeypatch, COMPLETE)
    files = {
        row["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for row in run["results"]
    }
    assert files == {"unparsed.py"}, "the value-level F019 should have been cleared"
    assert "unparsed.py" in capsys.readouterr().out


def test_incomplete_matrix_clears_nothing(repo, monkeypatch):
    run = _run(repo, monkeypatch, INCOMPLETE)
    files = {
        row["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for row in run["results"]
    }
    assert files == {"handler.py", "unparsed.py"}


def test_static_run_clears_nothing(repo):
    output = repo / "static.sarif"
    main(
        ["--repo", str(repo), "--rule", "F019", "--output", str(output), "--exit-zero"]
    )
    run = json.loads(output.read_text())["runs"][0]
    files = {
        row["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for row in run["results"]
    }
    assert files == {"handler.py", "unparsed.py"}
    assert run["properties"].get("atlan/preflightTests", {}).get("F016", {}).get(
        "execution"
    ) in (None, "not_evaluated")


def test_f019_alone_still_runs_the_scenario_leg_it_depends_on(repo, monkeypatch):
    """``--rule F019 --with-tests`` must execute F016, or it has no evidence."""
    seen: list[set[str]] = []

    from conformance.suite.checks.preflight import _behavior

    def record(root, rule_ids=None, *a, **k):
        seen.append(set(rule_ids or ()))
        return _behavior.BehaviorResult([], dict(COMPLETE))

    monkeypatch.setattr(_behavior, "run_behavior", record)
    main(
        [
            "--repo",
            str(repo),
            "--rule",
            "F019",
            "--with-tests",
            "--output",
            str(repo / "report.sarif"),
            "--exit-zero",
        ]
    )
    assert seen and all("F016" in ids for ids in seen)


# --- end to end, with no stub between the scenario and the verdict ----------

SCENARIO = """
import pytest
from application_sdk.handler.contracts import PreflightOutput, PreflightCheck, PreflightStatus
from conformance.preflight_testing import assert_preflight_result


@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")
def test_healthy():
    result = PreflightOutput(
        status=PreflightStatus.READY,
        checks=[PreflightCheck(name="connection", passed=True)],
    )
    assert_preflight_result(
        result,
        required_checks={"connection"},
        observed_checks={"connection"},
        expected_status="ready",
    )
"""


def test_real_passing_matrix_clears_the_value_level_gap(repo, monkeypatch):
    """No stub: pytest really runs, the summary is really complete, F019 drops.

    The matrix is narrowed to one scenario so the fixture repo does not have to
    ship all thirteen; everything downstream of that — the subprocess, the
    report, the ``complete`` verdict, the clearing pass — is the real path.
    """
    from conformance.preflight_testing import SCENARIOS

    monkeypatch.setitem(SCENARIOS, "F016", ("healthy",))
    (repo / "tests").mkdir()
    (repo / "tests" / "test_contract.py").write_text(SCENARIO)
    output = repo / "e2e.sarif"
    main(
        [
            "--repo",
            str(repo),
            "--rule",
            "F019",
            "--with-tests",
            "--output",
            str(output),
            "--exit-zero",
        ]
    )
    run = json.loads(output.read_text())["runs"][0]
    summary = run["properties"]["atlan/preflightTests"]["F016"]
    assert (summary["execution"], summary["passed"], summary["complete"]) == (
        "completed",
        1,
        True,
    )
    files = {
        row["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for row in run["results"]
    }
    assert files == {"unparsed.py"}
