"""Tests for every categorical leaf — category, default_retryable, code, audience, and fields."""

import pytest

from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AlreadyExistsError,
    AppPermissionDeniedError,
    AppTimeoutError,
    AuthError,
    CancelledError,
    DataIntegrityError,
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    NotFoundError,
    PreconditionError,
    RateLimitedError,
    ResourceExhaustedError,
    UnimplementedError,
)

_LEAVES = [
    (
        CancelledError,
        FailureCategory.CANCELLED,
        False,
        "CANCELLED",
        Audience.UNKNOWN,
        ["cancelled_by", "reason"],
    ),
    (
        AppTimeoutError,
        FailureCategory.TIMEOUT,
        True,
        "TIMEOUT",
        Audience.UNKNOWN,
        ["operation", "timeout_seconds", "elapsed_seconds"],
    ),
    (
        RateLimitedError,
        FailureCategory.RATE_LIMITED,
        True,
        "RATE_LIMITED",
        Audience.USER,
        ["limit_type", "retry_after_seconds", "quota_name"],
    ),
    (
        AuthError,
        FailureCategory.AUTH,
        False,
        "AUTH",
        Audience.USER,
        ["auth_method", "principal", "failure_reason"],
    ),
    (
        AppPermissionDeniedError,
        FailureCategory.PERMISSION,
        False,
        "PERMISSION",
        Audience.USER,
        ["principal", "resource", "required_action"],
    ),
    (
        NotFoundError,
        FailureCategory.NOT_FOUND,
        False,
        "NOT_FOUND",
        Audience.USER,
        ["resource_type", "resource_identifier"],
    ),
    (
        AlreadyExistsError,
        FailureCategory.ALREADY_EXISTS,
        False,
        "ALREADY_EXISTS",
        Audience.USER,
        ["resource_type", "resource_identifier"],
    ),
    (
        InvalidInputError,
        FailureCategory.INVALID_INPUT,
        False,
        "INVALID_INPUT",
        Audience.USER,
        ["field", "constraint", "value_summary"],
    ),
    (
        PreconditionError,
        FailureCategory.PRECONDITION,
        False,
        "PRECONDITION",
        Audience.USER,
        ["resource", "expected_state", "actual_state"],
    ),
    (
        DependencyUnavailableError,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
        True,
        "DEPENDENCY_UNAVAILABLE",
        Audience.PLATFORM,
        ["service", "target", "network_error"],
    ),
    (
        ResourceExhaustedError,
        FailureCategory.RESOURCE_EXHAUSTED,
        True,
        "RESOURCE_EXHAUSTED",
        Audience.PLATFORM,
        ["resource", "limit", "observed"],
    ),
    (
        DataIntegrityError,
        FailureCategory.DATA_INTEGRITY,
        False,
        "DATA_INTEGRITY",
        Audience.FRAMEWORK,
        ["expectation", "observed", "location"],
    ),
    (
        InternalError,
        FailureCategory.INTERNAL,
        False,
        "INTERNAL",
        Audience.FRAMEWORK,
        ["component", "invariant", "classification_pending"],
    ),
    (
        UnimplementedError,
        FailureCategory.UNIMPLEMENTED,
        False,
        "UNIMPLEMENTED",
        Audience.FRAMEWORK,
        ["operation", "reason"],
    ),
]


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_metadata(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    assert cls.category is expected_cat
    assert cls.default_retryable is expected_retryable
    assert cls.code == expected_code
    assert cls.audience is expected_audience


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_has_expected_fields(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(cls)}
    for field in expected_fields:
        assert field in field_names, f"{cls.__name__} missing field '{field}'"


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_constructs_with_message_only(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    e = cls(message="test")
    assert str(e) == "test"
    assert e.category is expected_cat


@pytest.mark.parametrize(
    "cls,expected_cat,expected_retryable,expected_code,expected_audience,expected_fields",
    _LEAVES,
)
def test_leaf_audience_in_failure_details(
    cls,
    expected_cat,
    expected_retryable,
    expected_code,
    expected_audience,
    expected_fields,
) -> None:
    fd = cls(message="test").to_failure_details()
    assert fd.audience is expected_audience


def test_internal_error_classification_pending_default() -> None:
    e = InternalError(message="bug")
    assert e.classification_pending is False


def test_internal_error_classification_pending_set() -> None:
    e = InternalError(message="unclassified failure", classification_pending=True)
    fd = e.to_failure_details()
    assert fd.evidence["classification_pending"] is True


def test_builtin_name_aliases_are_canonical_classes() -> None:
    from application_sdk.errors import PermissionError as SDKPermissionError
    from application_sdk.errors import TimeoutError as SDKTimeoutError

    assert SDKTimeoutError is AppTimeoutError
    assert SDKPermissionError is AppPermissionDeniedError
