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
    result = run_behavior(tmp_path, {"P062"})
    assert result.findings
    assert result.summary["P062"]["evaluated"] == 0
    assert result.summary["P062"]["missing"]


def test_selected_pass_and_missing_coverage(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="P062", scenario="healthy", entrypoint="default")\ndef test_healthy():\n    exercise_healthy()\n',
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.summary["P062"]["evaluated"] == 1
    assert result.summary["P062"]["passed"] == 1
    assert result.findings


@pytest.mark.parametrize(
    "outcome", ['pytest.skip("unavailable")', 'pytest.xfail("pending")', "assert False"]
)
def test_nonpassing_scenario_never_counts_as_pass(tmp_path, outcome):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="P062", scenario="healthy", entrypoint="default")\ndef test_healthy():\n    '
        + outcome
        + "\n",
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.summary["P062"]["passed"] == 0
    assert any("healthy" in f.message for f in result.findings)


def test_timeout_is_bounded_and_reported(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="P062", scenario="healthy", entrypoint="default")\ndef test_healthy():\n    import time\n    time.sleep(60)\n',
    )
    result = run_behavior(tmp_path, {"P062"}, timeout=1)
    assert result.summary["P062"]["execution"] == "timeout"
    assert result.findings


def test_complete_selected_matrix(tmp_path, monkeypatch):
    from conformance.preflight_testing import SCENARIOS

    monkeypatch.setitem(SCENARIOS, "P062", ("healthy",))
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="P062", scenario="healthy")\ndef test_healthy():\n    exercise_healthy()\n',
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.findings == []
    assert result.summary["P062"]["passed"] == 1


def test_setup_failure_is_not_evaluated(tmp_path):
    write_test(
        tmp_path,
        '@pytest.fixture\ndef broken():\n    raise RuntimeError("synthetic failure")\n@pytest.mark.preflight_conformance(rule="P062", scenario="healthy", entrypoint="default")\ndef test_healthy(broken):\n    pass\n',
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.summary["P062"]["evaluated"] == 0
    assert result.summary["P062"]["passed"] == 0


def test_sdk_rule_requires_registered_scenarios(tmp_path):
    result = run_behavior(tmp_path, {"P063", "P064"}, scope="sdk")
    assert {f.rule_id for f in result.findings} == {"P063", "P064"}


def test_marker_without_contract_assertion_is_not_evidence(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="P062", scenario="healthy")\ndef test_empty():\n    pass\n',
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.summary["P062"]["passed"] == 0


def test_xpass_does_not_certify_scenario(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.xfail(reason="pending")\n@pytest.mark.preflight_conformance(rule="P062", scenario="healthy")\ndef test_empty():\n    pass\n',
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.summary["P062"]["passed"] == 0


def test_lifetime_scenario_requires_lifetime_assertion(tmp_path):
    write_test(
        tmp_path,
        '@pytest.mark.preflight_conformance(rule="P062", scenario="hung_probe")\ndef test_hung():\n    exercise_healthy()\n',
    )
    result = run_behavior(tmp_path, {"P062"})
    assert result.summary["P062"]["passed"] == 0
