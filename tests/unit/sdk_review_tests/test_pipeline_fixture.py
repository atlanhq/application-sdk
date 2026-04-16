"""Companion test for pipeline_test_fixture."""

import logging

from application_sdk.utils.pipeline_test_fixture import (
    debug_trace,
    load_config,
    process_event,
    transform_record,
)


def test_transform_record():
    result = transform_record({"a": 1, "b": 2})
    assert result == {"a": "1", "b": "2"}


def test_transform_record_empty():
    result = transform_record({})
    assert result == {}


def test_transform_record_none_values():
    result = transform_record({"x": None, "y": True})
    assert result == {"x": "None", "y": "True"}


def test_load_config(tmp_path):
    config_file = tmp_path / "test.cfg"
    config_file.write_text("key=value")
    result = load_config(str(config_file))
    assert result == "key=value"


def test_load_config_missing_file():
    result = load_config("/nonexistent/path/config.cfg")
    assert result is None


def test_process_event(caplog):
    with caplog.at_level(logging.INFO):
        process_event("login", "user_42")
    assert "event=login user=user_42" in caplog.text


def test_debug_trace(caplog):
    with caplog.at_level(logging.DEBUG):
        debug_trace({"step": 1})
    assert "TRACE:" in caplog.text
    assert "step" in caplog.text
