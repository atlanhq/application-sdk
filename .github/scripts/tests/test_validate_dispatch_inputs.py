"""Tests for .github/scripts/validate_dispatch_inputs.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_dispatch_inputs.py"
_spec = importlib.util.spec_from_file_location("validate_dispatch_inputs", _MODULE_PATH)
assert _spec and _spec.loader
validate_dispatch_inputs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validate_dispatch_inputs)

validate = validate_dispatch_inputs.validate
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
