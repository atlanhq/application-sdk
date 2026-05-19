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
        Audience.APP_OWNER,
        ["cancelled_by", "reason"],
    ),
    (
        AppTimeoutError,
        FailureCategory.TIMEOUT,
        True,
        "TIMEOUT",
        Audience.APP_OWNER,
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
        Audience.APP_OWNER,
        ["expectation", "observed", "location"],
    ),
    (
        InternalError,
        FailureCategory.INTERNAL,
        False,
        "INTERNAL",
        Audience.APP_OWNER,
        ["component", "invariant", "classification_pending"],
    ),
    (
        UnimplementedError,
        FailureCategory.UNIMPLEMENTED,
        False,
        "UNIMPLEMENTED",
        Audience.APP_OWNER,
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


# ---------------------------------------------------------------------------
# EC1 — FailureCategory.http_status maps to RFC-standard codes
# ---------------------------------------------------------------------------

_CATEGORY_HTTP_STATUS = [
    (FailureCategory.CANCELLED, 499),
    (FailureCategory.TIMEOUT, 504),
    (FailureCategory.RATE_LIMITED, 429),
    (FailureCategory.AUTH, 401),
    (FailureCategory.PERMISSION, 403),
    (FailureCategory.NOT_FOUND, 404),
    (FailureCategory.ALREADY_EXISTS, 409),
    (FailureCategory.INVALID_INPUT, 422),
    (FailureCategory.PRECONDITION, 409),
    (FailureCategory.DEPENDENCY_UNAVAILABLE, 503),
    (FailureCategory.RESOURCE_EXHAUSTED, 503),
    (FailureCategory.DATA_INTEGRITY, 500),
    (FailureCategory.INTERNAL, 500),
    (FailureCategory.UNIMPLEMENTED, 501),
]


@pytest.mark.parametrize("category,expected_status", _CATEGORY_HTTP_STATUS)
def test_failure_category_http_status(
    category: FailureCategory, expected_status: int
) -> None:
    assert category.http_status == expected_status


def test_every_failure_category_has_http_status() -> None:
    """Guard: adding a new FailureCategory without updating the http_status map raises KeyError."""
    for category in FailureCategory:
        assert isinstance(category.http_status, int), (
            f"{category} missing from http_status map"
        )


# ---------------------------------------------------------------------------
# EC2 — AppError.http_status delegates to its category
# ---------------------------------------------------------------------------


def test_app_error_http_status_permission_is_403() -> None:
    assert AppPermissionDeniedError(message="no access").http_status == 403


def test_app_error_http_status_auth_is_401() -> None:
    assert AuthError(message="invalid token").http_status == 401


def test_app_error_http_status_dependency_unavailable_is_503() -> None:
    assert DependencyUnavailableError(message="db down").http_status == 503


def test_app_error_http_status_invalid_input_is_422() -> None:
    assert InvalidInputError(message="bad field").http_status == 422


def test_app_error_http_status_not_found_is_404() -> None:
    assert NotFoundError(message="missing resource").http_status == 404


def test_app_error_http_status_internal_is_500() -> None:
    assert InternalError(message="unexpected").http_status == 500
