from pathlib import Path

import pytest
from conformance.suite.checks.preflight import _untyped_failure
from conformance.suite.checks.preflight._common import build_registry


def findings(tmp_path: Path, body: str, imports: str = ""):
    path = tmp_path / "handler.py"
    path.write_text(imports + "\n" + body)
    return _untyped_failure.scan(build_registry([path], tmp_path))


@pytest.mark.parametrize("expression", ["ok", "not failed"])
def test_dynamic_verdict_reports_unresolved(tmp_path, expression):
    result = findings(
        tmp_path,
        f'def make(ok, failed):\n    return PreflightCheck(name="probe", passed={expression})\n',
        "from application_sdk.handler.contracts import PreflightCheck",
    )
    assert [finding.rule_id for finding in result] == ["P065"]


@pytest.mark.parametrize(
    "imports,constructor",
    [
        ("from application_sdk.handler import PreflightCheck as Check", "Check"),
        ("import application_sdk.handler as handler", "handler.PreflightCheck"),
        ("import application_sdk.handler", "application_sdk.handler.PreflightCheck"),
    ],
)
def test_public_reexport_is_recognized(tmp_path, imports, constructor):
    result = findings(
        tmp_path,
        f'def make():\n    return {constructor}(name="probe", passed=False)\n',
        imports,
    )
    assert [finding.rule_id for finding in result] == ["P034"]


def test_invalid_status_does_not_replace_passed(tmp_path):
    result = findings(
        tmp_path,
        'def make():\n    return PreflightCheck(name="probe", status="READY")\n',
        "from application_sdk.handler import PreflightCheck",
    )
    assert [finding.rule_id for finding in result] == ["P034"]


def test_local_true_binding_is_clean(tmp_path):
    result = findings(
        tmp_path,
        'def make():\n    ok = True\n    return PreflightCheck(name="probe", passed=ok)\n',
        "from application_sdk.handler import PreflightCheck",
    )
    assert result == []


def test_negated_local_true_is_failed(tmp_path):
    result = findings(
        tmp_path,
        'def make():\n    failed = True\n    return PreflightCheck(name="probe", passed=not failed)\n',
        "from application_sdk.handler import PreflightCheck",
    )
    assert [finding.rule_id for finding in result] == ["P034"]


def test_dynamic_verdict_with_error_is_clean(tmp_path):
    result = findings(
        tmp_path,
        'def make(ok, error):\n    return PreflightCheck(name="probe", passed=ok, error=error)\n',
        "from application_sdk.handler import PreflightCheck",
    )
    assert result == []


def test_unrelated_module_is_not_recognized(tmp_path):
    result = findings(
        tmp_path,
        'def make():\n    return PreflightCheck(name="probe")\n',
        "from another.handler import PreflightCheck",
    )
    assert result == []
