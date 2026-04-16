"""Companion test for pipeline_test_fixture."""

from application_sdk.utils.pipeline_test_fixture import transform_record


def test_transform_record():
    result = transform_record({"a": 1, "b": 2})
    assert result == {"a": "1", "b": "2"}
