from conformance.suite.checks.preflight._common import build_registry
from conformance.suite.checks.preflight._contracts import scan


def check(tmp_path, source):
    p = tmp_path / "handler.py"
    p.write_text(
        "from application_sdk.handler import Handler\nfrom application_sdk.handler.contracts import PreflightInput, PreflightOutput, PreflightCheck\nfrom application_sdk.errors import AuthError, InternalError\n"
        + source
    )
    return [f.rule_id for f in scan(build_registry([p], tmp_path))]


def test_raised_only_error_not_guidance_finding(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  raise AuthError(message="Failed")\n',
    )
    assert "P053" not in ids


def test_replaced_classifier_error_not_guidance_finding(tmp_path):
    ids = check(
        tmp_path,
        'def classify():\n return InternalError(message="Unknown")\ndef final_error():\n original = classify()\n return AuthError(message="Failed", suggested_action="Check access.")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=final_error().to_failure_details())])\n',
    )
    assert "P053" not in ids


def test_shared_output_helper_tracks_argument(tmp_path):
    ids = check(
        tmp_path,
        'def failed(error):\n return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return failed(AuthError(message="Failed"))\n',
    )
    assert "P053" in ids


def test_raised_classifier_branch_excluded(tmp_path):
    ids = check(
        tmp_path,
        'def classify(value):\n if value:\n  return InternalError(message="Unknown")\n return AuthError(message="Failed", suggested_action="Check access.")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = classify(input)\n  if isinstance(error, InternalError):\n   raise error\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\n',
    )
    assert "P053" not in ids


def test_indirect_missing_action_is_reported(tmp_path):
    ids = check(
        tmp_path,
        'def failure():\n return AuthError(message="Failed")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = failure()\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\n',
    )
    assert "P053" in ids


def test_conditional_guard_does_not_hide_failure(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = InternalError(message="Failed")\n  if input:\n   if isinstance(error, InternalError):\n    raise error\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=error.to_failure_details())])\n',
    )
    assert "P053" in ids


def test_computed_aggregation_is_unresolved(tmp_path):
    ids = check(
        tmp_path,
        "class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  checks = await probe()\n  return PreflightOutput(status=compute(checks), checks=checks)\n",
    )
    assert "P065" in ids
    assert "P055" not in ids


def test_invalid_status_field_is_reported(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(status="ready")])\n',
    )
    assert "P052" in ids


def test_forwarded_action_cannot_silently_pass(tmp_path):
    ids = check(
        tmp_path,
        'def failure(action):\n return AuthError(message="Failed", suggested_action=action)\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=failure(None).to_failure_details())])\n',
    )
    assert "P065" in ids


def test_expanded_error_kwargs_cannot_silently_pass(tmp_path):
    ids = check(
        tmp_path,
        'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  values = {"message": "Failed"}\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=AuthError(**values).to_failure_details())])\n',
    )
    assert "P065" in ids


def test_unreachable_error_factory_branch_not_reported(tmp_path):
    ids = check(
        tmp_path,
        'def failure():\n if False:\n  return AuthError(message="Failed")\n return AuthError(message="Failed", suggested_action="Check access.")\nclass H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=failure().to_failure_details())])\n',
    )
    assert "P053" not in ids


def test_adding_guidance_clears_only_guidance_finding(tmp_path):
    source = 'class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  return PreflightOutput(checks=[PreflightCheck(passed=False, error=AuthError(message="Failed").to_failure_details())])\n'
    assert "P053" in check(tmp_path, source)
    corrected = source.replace(
        'message="Failed"', 'message="Failed", suggested_action="Check access."'
    )
    assert "P053" not in check(tmp_path, corrected)


def test_success_with_resolved_none_is_clean(tmp_path):
    ids = check(
        tmp_path,
        "class H(Handler):\n async def preflight_check(self, input: PreflightInput) -> PreflightOutput:\n  error = None\n  return PreflightOutput(checks=[PreflightCheck(passed=True, error=error)])\n",
    )
    assert ids == []
