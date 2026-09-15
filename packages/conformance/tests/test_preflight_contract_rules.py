from pathlib import Path

import pytest
from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._contracts import scan

IMPORTS = """from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import PreflightInput, PreflightOutput, PreflightCheck
from application_sdk.errors import AuthError, FailureDetails
"""


def check(
    tmp_path: Path,
    body: str,
    extra: str = "",
    signature: str = "self, input: PreflightInput) -> PreflightOutput",
) -> list[str]:
    path = tmp_path / "handler.py"
    path.write_text(
        IMPORTS
        + extra
        + "\nclass H(Handler):\n    async def preflight_check("
        + signature
        + ":\n"
        + "\n".join("        " + line for line in body.splitlines())
        + "\n"
    )
    return [f.rule_id for f in scan(build_registry([path], tmp_path))]


@pytest.mark.parametrize("signature", ["self, input)", "self, input: dict) -> dict"])
def test_handler_contract_rejects_untyped(tmp_path: Path, signature: str) -> None:
    assert "P052" in check(tmp_path, "return None", signature=signature)


def test_handler_contract_alias(tmp_path: Path) -> None:
    assert (
        check(
            tmp_path,
            "return PreflightOutput(checks=[])",
            "from application_sdk.handler.contracts import PreflightInput as Request, PreflightOutput as Result\n",
            "self, input: Request) -> Result",
        )
        == []
    )


def test_handler_legacy_result(tmp_path: Path) -> None:
    assert "P052" in check(tmp_path, 'return {"success": True}')


@pytest.mark.parametrize("action", ["None", '"  "'])
def test_blank_failure_action(tmp_path: Path, action: str) -> None:
    assert "P053" in check(
        tmp_path,
        f'return PreflightOutput(checks=[PreflightCheck(passed=False, error=FailureDetails(message="Probe failed", suggested_action={action}))])',
    )


def test_missing_failure_action(tmp_path: Path) -> None:
    assert "P053" in check(
        tmp_path,
        'return PreflightOutput(checks=[PreflightCheck(passed=False, error=FailureDetails(message="Probe failed"))])',
    )


def test_inherited_action(tmp_path: Path) -> None:
    extra = 'class ProbeError(AuthError):\n    message: str = "Probe failed"\n    suggested_action: str = "Check the configured permission."\n'
    assert (
        check(
            tmp_path,
            "return PreflightOutput(checks=[PreflightCheck(passed=False, error=ProbeError().to_failure_details())])",
            extra,
        )
        == []
    )


def test_unknown_factory_action(tmp_path: Path) -> None:
    assert check(
        tmp_path,
        "return PreflightOutput(checks=[PreflightCheck(passed=False, error=external_factory())])",
    ) == ["P065"]


def test_expected_error_escapes(tmp_path: Path) -> None:
    assert "P054" in check(tmp_path, 'raise AuthError(message="Probe failed")')


def test_helper_error_escapes(tmp_path: Path) -> None:
    assert "P054" in check(
        tmp_path,
        "return probe()",
        'def probe():\n    raise AuthError(message="Probe failed")\n',
    )


def test_helper_error_converted(tmp_path: Path) -> None:
    assert "P054" not in check(
        tmp_path,
        "try:\n    return probe()\nexcept AuthError as exc:\n    return PreflightOutput(checks=[PreflightCheck(passed=False, error=exc.to_failure_details())])",
        'def probe():\n    raise AuthError(message="Probe failed")\n',
    )


def test_unrelated_raise_not_probe_failure(tmp_path: Path) -> None:
    assert "P054" not in check(tmp_path, 'raise RuntimeError("Unexpected invariant")')


def test_ready_failed_check(tmp_path: Path) -> None:
    assert "P055" in check(
        tmp_path,
        'return PreflightOutput(status="READY", checks=[PreflightCheck(passed=False, error=external_factory())])',
    )


def test_not_ready_successful_checks(tmp_path: Path) -> None:
    assert "P055" in check(
        tmp_path,
        'return PreflightOutput(status="NOT_READY", checks=[PreflightCheck(passed=True)])',
    )


def test_partial_failed_check_is_valid(tmp_path: Path) -> None:
    assert check(
        tmp_path,
        'return PreflightOutput(status="PARTIAL", checks=[PreflightCheck(passed=False, error=external_factory())])',
    ) == ["P066", "P065"]


def test_interactive_input_without_entrypoint_valid(tmp_path: Path) -> None:
    assert (
        check(
            tmp_path,
            "request = PreflightInput(credentials=[])\nreturn PreflightOutput(checks=[])",
        )
        == []
    )


def test_workflow_input_wrong_entrypoint(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        IMPORTS
        + 'from application_sdk.app import entrypoint\n@entrypoint(name="crawl")\nasync def crawl(input):\n    return PreflightInput(entrypoint="mine")\n'
    )
    assert [f.rule_id for f in scan(build_registry([path], tmp_path))] == ["P056"]


def test_workflow_input_correct_entrypoint(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text(
        IMPORTS
        + 'from application_sdk.app import entrypoint\n@entrypoint(name="crawl")\nasync def crawl(input):\n    return PreflightInput(entrypoint="crawl")\n'
    )
    assert scan(build_registry([path], tmp_path)) == []


def test_imported_failure_inherits_missing_action(tmp_path: Path):
    errors = tmp_path / "failures.py"
    errors.write_text(
        "from application_sdk.errors import AuthError\nclass ProbeDenied(AuthError):\n    pass\n"
    )
    handler = tmp_path / "handler.py"
    handler.write_text(
        IMPORTS
        + 'from failures import ProbeDenied\nclass H(Handler):\n    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n        return PreflightOutput(status="not_ready", checks=[PreflightCheck(name="auth", passed=False, error=ProbeDenied(message="Permission denied").to_failure_details())])\n'
    )
    assert "P053" in {
        f.rule_id for f in scan(build_registry([errors, handler], tmp_path))
    }


def test_imported_classifier_raise_is_detected(tmp_path: Path):
    helper = tmp_path / "failures.py"
    helper.write_text(
        'from application_sdk.errors import RateLimitedError\ndef classify():\n    raise RateLimitedError(message="Try later")\n'
    )
    handler = tmp_path / "handler.py"
    handler.write_text(
        IMPORTS
        + "from failures import classify\nclass H(Handler):\n    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n        return classify()\n"
    )
    assert "P054" in {
        f.rule_id for f in scan(build_registry([helper, handler], tmp_path))
    }


def test_unrelated_exception_catch_does_not_hide_raise(tmp_path: Path):
    assert "P054" in check(
        tmp_path,
        'try:\n    raise AuthError(message="Denied")\nexcept ValueError:\n    return PreflightOutput(status="ready")',
    )


@pytest.mark.parametrize(
    "catch, raised", [("Exception", ""), ("AuthError as exc", "exc")]
)
def test_typed_failure_reraise(tmp_path, catch, raised):
    assert "P054" in check(
        tmp_path,
        f'try:\n    raise AuthError(message="Failure")\nexcept {catch}:\n    raise {raised}',
    )


@pytest.mark.parametrize("name", ["ObjectStoreReadError", "DiskFullError"])
def test_sdk_default_action_not_missing(tmp_path, name):
    assert "P053" not in check(
        tmp_path, f"raise {name}()", f"from application_sdk.errors import {name}\n"
    )


@pytest.mark.parametrize(
    "status",
    [
        '"partial"',
        '"PARTIAL"',
        "PreflightStatus.PARTIAL",
        '"partial" if degraded else "ready"',
    ],
)
def test_partial_preflight_is_deprecated(tmp_path, status):
    assert "P066" in check(
        tmp_path, f"return PreflightOutput(status={status}, checks=[])"
    )


@pytest.mark.parametrize("status", ['"ready"', '"not_ready"'])
def test_supported_preflight_status_is_not_deprecated(tmp_path, status):
    assert "P066" not in check(
        tmp_path, f"return PreflightOutput(status={status}, checks=[])"
    )
