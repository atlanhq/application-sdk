import pytest
from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._contracts import scan

IMPORTS = "from application_sdk.handler import Handler\nfrom application_sdk.handler.contracts import PreflightInput, PreflightOutput, PreflightCheck\nfrom application_sdk.errors import AppError, AuthError, InternalError\n"


def findings(tmp_path, source):
    p = tmp_path / "handler.py"
    p.write_text(IMPORTS + source)
    return scan(build_registry([p], tmp_path))


def check(tmp_path, source):
    return [f.rule_id for f in findings(tmp_path, source)]


def caught(tmp_path, clause, body):
    """Scan a handler whose failed row is built inside ``except <clause>``."""
    return findings(
        tmp_path,
        "class H(Handler):\n"
        " async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n"
        "  try:\n"
        "   await probe()\n"
        f"  except {clause}:\n"
        + "".join(f"   {line}\n" for line in body.splitlines())
        + "  return PreflightOutput(checks=[PreflightCheck(passed=True)])\n",
    )


FAILED_ROW = "return PreflightOutput(checks=[PreflightCheck(passed=False, error=exc.to_failure_details())])"


def test_raised_only_error_not_guidance_finding(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  raise AuthError(message="Failed")\n',
    )
    assert "F007" not in ids


def test_replaced_classifier_error_not_guidance_finding(tmp_path):
    ids = check(
        tmp_path,
        'def classify():\n return InternalError(message="Unknown")\ndef final_error():\n original = classify()\n return AuthError(message="Failed", suggested_action="Check access.")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=final_error().to_failure_details())])\n',
    )
    assert "F007" not in ids


def test_shared_output_helper_tracks_argument(tmp_path):
    ids = check(
        tmp_path,
        'def failed(error):\n return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return failed(AuthError(message="Failed"))\n',
    )
    assert "F007" in ids


def test_raised_classifier_branch_excluded(tmp_path):
    ids = check(
        tmp_path,
        'def classify(value):\n if value:\n  return InternalError(message="Unknown")\n return AuthError(message="Failed", suggested_action="Check access.")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = classify(input)\n  if isinstance(error, InternalError):\n   raise error\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\n',
    )
    assert "F007" not in ids


def test_indirect_missing_action_is_reported(tmp_path):
    ids = check(
        tmp_path,
        'def failure():\n return AuthError(message="Failed")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = failure()\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\n',
    )
    assert "F007" in ids


def test_conditional_guard_does_not_hide_failure(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = InternalError(message="Failed")\n  if input:\n   if isinstance(error, InternalError):\n    raise error\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\n',
    )
    assert "F007" in ids


def test_computed_aggregation_is_unresolved(tmp_path):
    ids = check(
        tmp_path,
        "class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  checks = await probe()\n  return PreflightOutput(status=compute(checks), checks=checks)\n",
    )
    assert "F019" in ids
    assert "F009" not in ids


def test_invalid_status_field_is_reported(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(status="ready")])\n',
    )
    assert "F006" in ids


def test_forwarded_action_cannot_silently_pass(tmp_path):
    ids = check(
        tmp_path,
        'def failure(action):\n return AuthError(message="Failed", suggested_action=action)\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=failure(None).to_failure_details())])\n',
    )
    assert "F019" in ids


def test_expanded_error_kwargs_cannot_silently_pass(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  values = {"message": "Failed"}\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=AuthError(**values).to_failure_details())])\n',
    )
    assert "F019" in ids


def test_unreachable_error_factory_branch_not_reported(tmp_path):
    ids = check(
        tmp_path,
        'def failure():\n if False:\n  return AuthError(message="Failed")\n return AuthError(message="Failed", suggested_action="Check access.")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=failure().to_failure_details())])\n',
    )
    assert "F007" not in ids


def test_adding_guidance_clears_only_guidance_finding(tmp_path):
    source = 'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=AuthError(message="Failed").to_failure_details())])\n'
    assert "F007" in check(tmp_path, source)
    corrected = source.replace(
        'message="Failed"', 'message="Failed", suggested_action="Check access."'
    )
    assert "F007" not in check(tmp_path, corrected)


def test_success_with_resolved_none_is_clean(tmp_path):
    ids = check(
        tmp_path,
        "class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = None\n  return PreflightOutput(checks=[PreflightCheck(passed=True, error=error)])\n",
    )
    assert ids == []


def test_caught_leaf_error_resolves(tmp_path):
    assert caught(tmp_path, "AuthError as exc", FAILED_ROW) == []


def test_caught_base_error_resolves(tmp_path):
    assert caught(tmp_path, "AppError as exc", FAILED_ROW) == []


def test_caught_tuple_resolves(tmp_path):
    assert caught(tmp_path, "(AuthError, InternalError) as exc", FAILED_ROW) == []


def test_caught_error_survives_intervening_statements(tmp_path):
    assert (
        caught(
            tmp_path,
            "AppError as exc",
            "transient = classify(exc)\nif transient is not None:\n raise transient\n"
            + FAILED_ROW,
        )
        == []
    )


@pytest.mark.parametrize(
    "clause", ["Exception as exc", "BaseException as exc", "ValueError as exc"]
)
def test_broad_catch_is_reported_as_narrowable(tmp_path, clause):
    reported = caught(tmp_path, clause, FAILED_ROW)
    assert [f.rule_id for f in reported] == ["F019"]
    assert "Narrow the clause" in reported[0].message


def test_caught_error_excluded_by_isinstance_reraise(tmp_path):
    ids = [
        f.rule_id
        for f in caught(
            tmp_path,
            "InternalError as exc",
            "if isinstance(exc, InternalError):\n raise exc\n" + FAILED_ROW,
        )
    ]
    assert "F019" not in ids


def test_dynamic_error_keeps_unresolved_message(tmp_path):
    reported = findings(
        tmp_path,
        "class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=build(input).to_failure_details())])\n",
    )
    assert [f.rule_id for f in reported] == ["F019"]
    assert "Failed-check error flow is unresolved" in reported[0].message


def test_caught_error_on_passed_row_is_failure_evidence(tmp_path):
    ids = [
        f.rule_id
        for f in caught(
            tmp_path,
            "AuthError as exc",
            "return PreflightOutput(checks=[PreflightCheck(passed=True, error=exc.to_failure_details())])",
        )
    ]
    assert ids == ["F009"]
