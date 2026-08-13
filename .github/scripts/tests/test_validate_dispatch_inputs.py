"""Tests for .github/scripts/validate_dispatch_inputs.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_dispatch_inputs.py"
_spec = importlib.util.spec_from_file_location("validate_dispatch_inputs", _MODULE_PATH)
assert _spec and _spec.loader
validate_dispatch_inputs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_dispatch_inputs)

validate = validate_dispatch_inputs.validate
rekey_for_attempt = validate_dispatch_inputs.rekey_for_attempt
DispatchInputsError = validate_dispatch_inputs.DispatchInputsError
main = validate_dispatch_inputs.main


def test_accepts_payload_carrying_distinct_id():
    validate('{"application_sdk_ref":"abc123","distinct_id":"abc123"}')


def test_accepts_distinct_id_alone():
    validate('{"distinct_id":"abc123"}')


def test_rejects_the_default_empty_object():
    """The action's own default -- the exact shape that broke #2923."""
    with pytest.raises(DispatchInputsError, match="missing a non-empty distinct_id"):
        validate("{}")


def test_rejects_payload_without_distinct_id():
    with pytest.raises(DispatchInputsError, match="missing a non-empty distinct_id"):
        validate('{"application_sdk_ref":"abc123"}')


@pytest.mark.parametrize("value", ["", " ", "   "])
def test_rejects_present_but_blank_distinct_id(value):
    """An empty id correlates nothing, so it fails the same way as absent."""
    with pytest.raises(DispatchInputsError, match="missing a non-empty distinct_id"):
        validate(f'{{"distinct_id":"{value}"}}')


def test_rejects_null_distinct_id():
    with pytest.raises(DispatchInputsError, match="missing a non-empty distinct_id"):
        validate('{"distinct_id":null}')


def test_rejects_malformed_json():
    with pytest.raises(DispatchInputsError, match="not valid JSON"):
        validate('{"distinct_id":')


def test_rejects_non_object_json():
    with pytest.raises(DispatchInputsError, match="must be a JSON object"):
        validate('["distinct_id"]')


def test_error_message_names_the_action_and_the_doc():
    """The message has to be actionable from the job log alone."""
    with pytest.raises(DispatchInputsError) as excinfo:
        validate("{}")
    message = str(excinfo.value)
    assert "return-dispatch v4" in message
    assert "docs/standards/connector-ci-e2e.md" in message


def test_main_exits_zero_on_valid_payload(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["validate_dispatch_inputs.py", "--workflow-inputs", '{"distinct_id":"abc"}'],
    )
    assert main() == 0


def test_main_exits_one_and_annotates_on_invalid_payload(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv", ["validate_dispatch_inputs.py", "--workflow-inputs", "{}"]
    )
    assert main() == 1
    assert "::error::" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# rekey_for_attempt — the retry must not collide with the first attempt.
#
# The receiver echoes distinct_id into a step name (return-dispatch locates the
# run by scanning those) and keys a cancel-in-progress concurrency group on it.
# A retry that reuses it can re-report the first run's failure, so these pin the
# divergence rather than trusting a comment.
# ---------------------------------------------------------------------------


def test_first_attempt_leaves_distinct_id_untouched():
    raw = '{"distinct_id": "abc123", "application_sdk_ref": "abc123"}'
    assert json.loads(rekey_for_attempt(raw, 1)) == json.loads(raw)


def test_attempt_zero_and_negative_are_treated_as_first_attempt():
    raw = '{"distinct_id": "abc123"}'
    assert json.loads(rekey_for_attempt(raw, 0))["distinct_id"] == "abc123"
    assert json.loads(rekey_for_attempt(raw, -1))["distinct_id"] == "abc123"


def test_retry_suffixes_distinct_id():
    raw = '{"distinct_id": "abc123"}'
    assert json.loads(rekey_for_attempt(raw, 2))["distinct_id"] == "abc123-attempt2"


def test_retry_distinct_id_differs_from_first_attempt():
    """The whole point: attempt 2 must not correlate to attempt 1's run."""
    raw = '{"distinct_id": "abc123"}'
    first = json.loads(rekey_for_attempt(raw, 1))["distinct_id"]
    second = json.loads(rekey_for_attempt(raw, 2))["distinct_id"]
    assert first != second


def test_retry_preserves_every_other_key():
    raw = '{"distinct_id": "abc123", "application_sdk_ref": "deadbeef", "base_image_ref": ""}'
    rekeyed = json.loads(rekey_for_attempt(raw, 2))
    assert rekeyed["application_sdk_ref"] == "deadbeef"
    assert rekeyed["base_image_ref"] == ""


def test_retry_output_is_still_a_valid_dispatch_payload():
    """A re-keyed payload must survive the same gate the first dispatch passes."""
    validate(rekey_for_attempt('{"distinct_id": "abc123"}', 2))


def test_rekey_rejects_payload_without_distinct_id():
    """Re-keying an absent key would invent a correlation id that correlates nothing."""
    with pytest.raises(DispatchInputsError):
        rekey_for_attempt("{}", 2)


def test_main_prints_rekeyed_payload_for_a_retry(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.argv",
        [
            "validate_dispatch_inputs.py",
            "--workflow-inputs",
            '{"distinct_id":"abc"}',
            "--attempt",
            "2",
        ],
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["distinct_id"] == "abc-attempt2"


def test_main_prints_nothing_on_the_first_attempt(monkeypatch, capsys):
    """Validation-only mode stays silent, so existing callers are unchanged."""
    monkeypatch.setattr(
        "sys.argv",
        ["validate_dispatch_inputs.py", "--workflow-inputs", '{"distinct_id":"abc"}'],
    )
    assert main() == 0
    assert capsys.readouterr().out == ""
