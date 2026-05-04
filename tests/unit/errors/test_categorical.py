"""Tests for every categorical leaf — category, default_retryable, code, and fields."""

import pytest

from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import (
    AuthError,
    CancelledError,
    DataIntegrityError,
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    NotFoundError,
    PermissionError,
    PreconditionError,
    RateLimitedError,
    ResourceExhaustedError,
    TimeoutError,
)

_LEAVES = [
    (
        CancelledError,
        FailureCategory.CANCELLED,
        False,
        "CANCELLED",
        ["cancelled_by", "reason"],
    ),
    (
        TimeoutError,
        FailureCategory.TIMEOUT,
        True,
        "TIMEOUT",
        ["operation", "timeout_seconds", "elapsed_seconds"],
    ),
    (
        RateLimitedError,
        FailureCategory.RATE_LIMITED,
        True,
        "RATE_LIMITED",
        ["limit_type", "retry_after_seconds", "quota_name"],
    ),
    (
        AuthError,
        FailureCategory.AUTH,
        False,
        "AUTH",
        ["auth_method", "principal", "failure_reason"],
    ),
    (
        PermissionError,
        FailureCategory.PERMISSION,
        False,
        "PERMISSION",
        ["principal", "resource", "required_action"],
    ),
    (
        NotFoundError,
        FailureCategory.NOT_FOUND,
        False,
        "NOT_FOUND",
        ["resource_type", "resource_identifier"],
    ),
    (
        InvalidInputError,
        FailureCategory.INVALID_INPUT,
        False,
        "INVALID_INPUT",
        ["field", "constraint", "value_summary"],
    ),
    (
        PreconditionError,
        FailureCategory.PRECONDITION,
        False,
        "PRECONDITION",
        ["resource", "expected_state", "actual_state"],
    ),
    (
        DependencyUnavailableError,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        True,
        "DEPENDENCY_UNAVAILABLE",
        ["service", "target", "network_error"],
    ),
    (
        ResourceExhaustedError,
        FailureCategory.RESOURCE_EXHAUSTED,
        True,
        "RESOURCE_EXHAUSTED",
        ["resource", "limit", "observed"],
    ),
    (
        DataIntegrityError,
        FailureCategory.DATA_INTEGRITY,
        False,
        "DATA_INTEGRITY",
        ["expectation", "observed", "location"],
    ),
    (
        InternalError,
        FailureCategory.INTERNAL,
        False,
        "INTERNAL",
        ["component", "invariant", "classification_pending"],
    ),
]


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_fields", _LEAVES
)
def test_leaf_metadata(
    cls, expected_cat, expected_retryable, expected_code, expected_fields
) -> None:
    assert cls.category is expected_cat
    assert cls.default_retryable is expected_retryable
    assert cls.code == expected_code


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_fields", _LEAVES
)
def test_leaf_has_expected_fields(
    cls, expected_cat, expected_retryable, expected_code, expected_fields
) -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(cls)}
    for field in expected_fields:
        assert field in field_names, f"{cls.__name__} missing field '{field}'"


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_fields", _LEAVES
)
def test_leaf_constructs_with_message_only(
    cls, expected_cat, expected_retryable, expected_code, expected_fields
) -> None:
    e = cls(message="test")
    assert str(e) == "test"
    assert e.category is expected_cat


def test_internal_error_classification_pending_default() -> None:
    e = InternalError(message="bug")
    assert e.classification_pending is False


def test_internal_error_classification_pending_set() -> None:
    e = InternalError(message="unclassified failure", classification_pending=True)
    fd = e.to_failure_details()
    assert fd.evidence["classification_pending"] is True
