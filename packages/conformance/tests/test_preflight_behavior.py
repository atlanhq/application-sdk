from pathlib import Path

import pytest
from conformance.suite.checks.preflight._behavior import run_behavior

HEALTHY = """
from application_sdk.handler.contracts import PreflightOutput, PreflightCheck, PreflightStatus
from conformance.preflight_testing import assert_preflight_result

def exercise_healthy():
    result = PreflightOutput(status=PreflightStatus.READY, checks=[PreflightCheck(name="connection", passed=True)])
    assert_preflight_result(result, required_checks={"connection"}, observed_checks={"connection"}, expected_status="ready")
"""


def write_test(root: Path, body: str) -> None:
    (root / "test_contract.py").write_text("import pytest\n" + HEALTHY + body)


def test_missing_scenarios_are_not_a_pass(tmp_path):
    write_test(tmp_path, "def test_unrelated():\n    assert False\n")
    result = run_behavior(tmp_path, {"F016"})
    assert result.findings
    assert result.summary["F016"]["evaluated"] == 0
    assert result.summary["F016"]["missing"]


def test_selected_pass_and_missing_coverage(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="default")\ndef test_healthy():\n    exercise_healthy()\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.summary["F016"]["evaluated"] == 1
    assert result.summary["F016"]["passed"] == 1
    assert result.findings


@pytest.mark.parametrize(
    "outcome", ['pytest.skip("unavailable")', 'pytest.xfail("pending")', "assert False"]
)
def test_nonpassing_scenario_never_counts_as_pass(tmp_path, outcome):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="default")\ndef test_healthy():\n    '
        + outcome
        + "\n",
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.summary["F016"]["passed"] == 0
    assert any("healthy" in f.message for f in result.findings)


def test_timeout_is_bounded_and_reported(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="default")\ndef test_healthy():\n    import time\n    time.sleep(60)\n',
    )
    result = run_behavior(tmp_path, {"F016"}, timeout=1)
    assert result.summary["F016"]["execution"] == "timeout"
    assert result.findings


def test_complete_selected_matrix(tmp_path, monkeypatch):
    from conformance.preflight_testing import SCENARIOS

    monkeypatch.setitem(SCENARIOS, "F016", ("healthy",))
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")\ndef test_healthy():\n    exercise_healthy()\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.findings == []
    assert result.summary["F016"]["passed"] == 1


def test_setup_failure_is_not_evaluated(tmp_path):
    write_test(
        tmp_path,
        '@pytest.fixture\ndef broken():\n    raise RuntimeError("synthetic failure")\n@pytest.mark.preflight_conformance(rule="F016", scenario="healthy", entrypoint="default")\ndef test_healthy(broken):\n    pass\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.summary["F016"]["evaluated"] == 0
    assert result.summary["F016"]["passed"] == 0


def test_sdk_rule_requires_registered_scenarios(tmp_path):
    result = run_behavior(tmp_path, {"F017", "F018"}, scope="sdk")
    assert {f.rule_id for f in result.findings} == {"F017", "F018"}


def test_marker_without_contract_assertion_is_not_evidence(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")\ndef test_empty():\n    pass\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.summary["F016"]["passed"] == 0


def test_xpass_does_not_certify_scenario(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.xfail(reason="pending")\n@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")\ndef test_empty():\n    pass\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.summary["F016"]["passed"] == 0


def test_lifetime_scenario_requires_lifetime_assertion(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="hung_probe")\ndef test_hung():\n    exercise_healthy()\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    assert result.summary["F016"]["passed"] == 0


def test_failed_scenario_points_to_recorded_test_location(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="F016", scenario="healthy")\ndef test_failure():\n    assert False\n',
    )
    result = run_behavior(tmp_path, {"F016"})
    finding = next(row for row in result.findings if "is failed" in row.message)
    assert finding.file == "test_contract.py"
    lines = (tmp_path / finding.file).read_text().splitlines()
    assert lines[finding.line - 1].startswith("@pytest.mark.preflight_conformance")


@pytest.mark.parametrize(
    "record",
    [
        {},
        {"file": "../outside.py", "line": 2},
        {"file": "test_contract.py", "line": 0},
        {"file": "test_contract.py", "line": True},
        {"file": "test_contract.py", "line": "2"},
    ],
)
def test_invalid_scenario_location_uses_fallback(tmp_path, record):
    from conformance.suite.checks.preflight._behavior import _record_location

    assert _record_location(record, tmp_path) == ("pyproject.toml", 1)
