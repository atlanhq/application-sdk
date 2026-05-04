"""Tests for app-side error subclassing patterns."""

import dataclasses

import pytest

from application_sdk.errors.base import AppError
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.errors.wire import FailureDetails


@dataclasses.dataclass(kw_only=True)
class STSAuthError(AuthError):
    role_arn: str
    sts_error_code: str | None = None
    auth_method: str = "iam_role"

    code = "ATHENA_STS_ASSUME_ROLE_FAILED"


def test_app_subclass_constructs_with_flat_kwargs() -> None:
    e = STSAuthError(message="STS failed", role_arn="arn:aws:iam::1234:role/MyRole")
    assert e.message == "STS failed"
    assert e.role_arn == "arn:aws:iam::1234:role/MyRole"
    assert e.auth_method == "iam_role"


def test_app_subclass_inherits_parent_fields() -> None:
    e = STSAuthError(message="x", role_arn="arn:aws:iam::1:role/R", principal="svc")
    assert e.principal == "svc"


def test_app_subclass_isinstance_checks() -> None:
    e = STSAuthError(message="x", role_arn="arn:aws:iam::1:role/R")
    assert isinstance(e, AuthError)
    assert isinstance(e, AppError)
    assert isinstance(e, Exception)


def test_app_subclass_failure_details_includes_all_non_base_fields() -> None:
    e = STSAuthError(
        message="STS failed",
        role_arn="arn:aws:iam::1234:role/MyRole",
        sts_error_code="AccessDenied",
    )
    fd = e.to_failure_details()
    assert isinstance(fd, FailureDetails)
    assert fd.category is FailureCategory.AUTH
    assert fd.code == "ATHENA_STS_ASSUME_ROLE_FAILED"
    # Parent leaf fields
    assert "auth_method" in fd.evidence
    assert fd.evidence["auth_method"] == "iam_role"
    # Child-specific fields
    assert fd.evidence["role_arn"] == "arn:aws:iam::1234:role/MyRole"
    assert fd.evidence["sts_error_code"] == "AccessDenied"


def test_app_subclass_qualified_code() -> None:
    e = STSAuthError(message="x", role_arn="arn")
    assert e.qualified_code == "AUTH.ATHENA_STS_ASSUME_ROLE_FAILED"


def test_app_subclass_effective_retryable_override() -> None:
    e = STSAuthError(message="x", role_arn="arn", retryable=True)
    assert e.effective_retryable is True


def test_raise_and_catch_by_parent() -> None:
    try:
        raise STSAuthError(message="STS failed", role_arn="arn:aws:iam::1:role/R")
    except AuthError as caught:
        assert caught.code == "ATHENA_STS_ASSUME_ROLE_FAILED"
    except Exception:
        pytest.fail("Should have been caught as AuthError")
