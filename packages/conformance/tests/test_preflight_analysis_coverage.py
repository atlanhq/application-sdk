"""F019 promises a fully defined scenario matrix clears a value-level gap. Prove it.

Two halves. First, that the checkers label each F019 correctly: a *value-level*
gap (the handler was analysed, one expression's value was not resolved) names
F016 in ``cleared_by``, a *structural* gap (the analysis never reached the code)
names nothing. Second, that the preflight pass acts on the label: when every
F016 scenario is defined it drops the first kind and keeps the second, and a
matrix that is anything less than fully defined drops neither. Nothing is
executed on either path; whether the scenarios pass is the test gate's measure.
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


@pytest.mark.parametrize(
    "binding",
    ["preflight_check = lookup()\n", "preflight_check: Callable = lookup()\n"],
    ids=["plain", "annotated"],
)
def test_dynamic_callback_binding_is_never_scenario_cleared(tmp_path, binding):
    reg = _registry(
        tmp_path,
        "from collections.abc import Callable\n"
        "async def _probe(input: PreflightInput) -> PreflightOutput:\n"
        "  return PreflightOutput(checks=[])\n" + binding,
    )
    findings = _f019(coverage_findings(reg))
    assert findings, "expected a dynamic-binding F019"
    assert all(f.cleared_by == frozenset() for f in findings)
    # It used to say "register behavioral scenarios", which clears nothing here.
    assert all("register behavioral scenarios" not in f.message for f in findings)
    assert all(
        "do not clear this" in f.message for f in findings
    ), "a finding no scenario can clear must say so rather than imply otherwise"


# --- a fully defined matrix clears the value-level gap ----------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo with one value-level F019 and one structural F019.

    The matrix is narrowed to one scenario so the fixture does not have to
    define all thirteen; the reader, the clearing pass and the report are the
    real path.
    """
    from conformance.preflight_scenarios import SCENARIOS

    monkeypatch.setitem(SCENARIOS, "F016", ("healthy",))
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-connector"\nversion="1.0.0"\n'
    )
    (tmp_path / "handler.py").write_text(
        IMPORTS
        + 'def check(ok):\n return PreflightCheck(name="probe", passed=ok)\n'
        + HEAD
        + "  return None\n"
    )
    (tmp_path / "unparsed.py").write_text("class H(:\n")
    return tmp_path


SCENARIO = """
import pytest
from conformance.preflight_testing import assert_preflight_result


{decorators}
def test_healthy():
    assert_preflight_result(result(), required_checks=set(), observed_checks=set(), expected_status="ready")
"""

REGISTERED = '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")'


def _define(repo, decorators=REGISTERED):
    (repo / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "unit" / "test_contract.py").write_text(
        SCENARIO.format(decorators=decorators)
    )


def _f019_files(repo, *extra):
    output = repo / "report.sarif"
    main(
        [
            "--repo",
            str(repo),
            "--rule",
            "F019",
            "--output",
            str(output),
            "--exit-zero",
            *extra,
        ]
    )
    return {
        row["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for row in json.loads(output.read_text())["runs"][0]["results"]
    }


def test_defined_matrix_clears_only_the_value_level_gap(repo):
    """F016 is computed even when the run asked only for F019."""
    _define(repo)
    assert _f019_files(repo) == {"unparsed.py"}


def test_undefined_matrix_clears_nothing(repo):
    assert _f019_files(repo) == {"handler.py", "unparsed.py"}


def test_skipped_registration_clears_nothing(repo):
    _define(repo, '@pytest.mark.skip(reason="later")\n' + REGISTERED)
    assert _f019_files(repo) == {"handler.py", "unparsed.py"}


def test_suppressed_scenario_gap_clears_nothing(repo):
    """A suppression hides a finding; it does not define the scenario."""
    _define(
        repo,
        REGISTERED
        + '\n@pytest.mark.skip(reason="later")\n# conformance: ignore[F016] tracked elsewhere',
    )
    assert _f019_files(repo) == {"handler.py", "unparsed.py"}


def test_clearing_never_executes_the_tests(repo, monkeypatch):
    """The registered test would fail if run; defining it is still enough."""
    import subprocess

    def _forbidden(*args, **kwargs):
        raise AssertionError("conformance must not start a subprocess for tests")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_contract.py").write_text(
        SCENARIO.format(decorators=REGISTERED).replace(
            "def test_healthy():\n", "def test_healthy():\n    assert False\n"
        )
    )
    assert _f019_files(repo) == {"unparsed.py"}
