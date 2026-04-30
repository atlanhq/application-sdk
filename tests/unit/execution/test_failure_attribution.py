"""Unit tests for failure attribution surfacing through ApplicationError.details.

Tests the helper that builds the structured-fields payload from a typed
exception. The activity wrapper at activities.py uses this to populate
ApplicationError.details so consumers (Automation Engine, dashboards,
OTel pipelines) can read structured failure attribution without parsing
the exception message string.

The convention requires the exception to carry ``category`` + ``code``
class attributes. The closed taxonomy of categories lives in
``application_sdk.observability.failures.FailureCategory``.
"""

from __future__ import annotations

import pytest

from application_sdk.app.base import NonRetryableError, RetryableError
from application_sdk.execution._temporal.activities import (
    _build_classified_failure_details,
)
from application_sdk.observability.failures import (
    DependencyRateLimitedError,
    DependencyUnavailableError,
    FailureCategory,
    PlatformBugError,
    SourceAuthFailedError,
    SourceUnreachableError,
    ThirdPartyRateLimitedError,
    ThirdPartyUnavailableError,
    TransientNetworkError,
    UnknownError,
)


# ---------------------------------------------------------------------------
# Test fixtures: example app-level typed exceptions following the convention
# ---------------------------------------------------------------------------


class _RedshiftAuthFailed(SourceAuthFailedError):
    code = "SOURCE_AUTH_FAILED"
    customer_message = (
        "Authentication to your Redshift cluster failed. "
        "Please verify the credentials configured in Atlan."
    )


class _MetastoreUnavailable(DependencyUnavailableError):
    code = "METASTORE_UNAVAILABLE"
    customer_message = None  # internal — engineering on-call


class _MetastoreRateLimited(DependencyRateLimitedError):
    code = "METASTORE_RATE_LIMITED"
    customer_message = None


class _PlainNonRetryable(NonRetryableError):
    """An existing-style NonRetryableError that does NOT follow the
    convention. Backwards-compat case: should produce an empty details list."""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildClassifiedFailureDetails:
    def test_typed_exception_emits_structured_payload(self) -> None:
        try:
            raise _RedshiftAuthFailed(
                internal_message="FATAL: password authentication failed for user atlan",
                evidence={"host": "warehouse.example.com", "pg_error": "28P01"},
            )
        except _RedshiftAuthFailed as exc:
            details = _build_classified_failure_details(exc)

        assert len(details) == 1
        payload = details[0]

        # Single-axis category replaces owner+sub_category.
        assert payload["category"] == "SOURCE_AUTH_FAILED"
        assert payload["code"] == "SOURCE_AUTH_FAILED"
        assert payload["customer_message"].startswith("Authentication to your")

        # Instance fields set at construction.
        assert payload["internal_message"].startswith("FATAL: password")
        assert payload["evidence"] == {
            "host": "warehouse.example.com",
            "pg_error": "28P01",
        }

        # Source/tenant come from APPLICATION_NAME / APP_TENANT_ID env-driven
        # constants (default to "default" in tests).
        assert "source_app" in payload
        assert "tenant_id" in payload

        # Owner field MUST be absent — direction is implicit in category.
        assert "owner" not in payload
        assert "sub_category" not in payload

    def test_cause_chain_preserved(self) -> None:
        original = ValueError("upstream parse failure")
        try:
            try:
                raise original
            except ValueError as v:
                raise _RedshiftAuthFailed(internal_message="wrapped failure") from v
        except _RedshiftAuthFailed as exc:
            details = _build_classified_failure_details(exc)

        assert len(details) == 1
        assert details[0]["cause"] is not None
        assert "upstream parse failure" in details[0]["cause"]

    def test_no_cause_yields_none(self) -> None:
        try:
            raise _RedshiftAuthFailed(internal_message="standalone failure")
        except _RedshiftAuthFailed as exc:
            details = _build_classified_failure_details(exc)

        assert details[0]["cause"] is None

    def test_plain_non_retryable_yields_empty_details(self) -> None:
        """Backwards compatibility: an existing NonRetryableError subclass
        that does not follow the convention produces no structured payload."""
        try:
            raise _PlainNonRetryable("boring failure")
        except _PlainNonRetryable as exc:
            details = _build_classified_failure_details(exc)

        assert details == []

    def test_generic_exception_yields_empty_details(self) -> None:
        try:
            raise ValueError("plain old value error")
        except ValueError as exc:
            details = _build_classified_failure_details(exc)

        assert details == []

    def test_evidence_defaults_to_empty_dict(self) -> None:
        try:
            raise _RedshiftAuthFailed(internal_message="oops")
        except _RedshiftAuthFailed as exc:
            details = _build_classified_failure_details(exc)

        assert details[0]["evidence"] == {}

    def test_customer_message_optional(self) -> None:
        """Internal-only failures (no customer-facing surface) carry None."""
        try:
            raise _MetastoreUnavailable(internal_message="our problem")
        except _MetastoreUnavailable as exc:
            details = _build_classified_failure_details(exc)

        assert details[0]["customer_message"] is None
        assert details[0]["category"] == "DEPENDENCY_UNAVAILABLE"
        assert details[0]["code"] == "METASTORE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Taxonomy contract — verify the SDK bases set the right category attributes
# ---------------------------------------------------------------------------


class TestTaxonomyContract:
    """The closed enum + typed bases are the SDK's contract with apps.
    These tests pin that contract."""

    @pytest.mark.parametrize(
        "base_class,expected_category",
        [
            (SourceAuthFailedError, FailureCategory.SOURCE_AUTH_FAILED),
            (SourceUnreachableError, FailureCategory.SOURCE_UNREACHABLE),
            (DependencyUnavailableError, FailureCategory.DEPENDENCY_UNAVAILABLE),
            (DependencyRateLimitedError, FailureCategory.DEPENDENCY_RATE_LIMITED),
            (ThirdPartyUnavailableError, FailureCategory.THIRD_PARTY_UNAVAILABLE),
            (ThirdPartyRateLimitedError, FailureCategory.THIRD_PARTY_RATE_LIMITED),
            (PlatformBugError, FailureCategory.PLATFORM_BUG),
            (TransientNetworkError, FailureCategory.TRANSIENT_NETWORK),
            (UnknownError, FailureCategory.UNKNOWN),
        ],
    )
    def test_base_sets_category(self, base_class, expected_category) -> None:
        assert base_class.category == expected_category

    def test_rate_limited_dependency_is_retryable(self) -> None:
        """DEPENDENCY_RATE_LIMITED multi-inherits RetryableError so Temporal
        retries with backoff."""
        assert issubclass(DependencyRateLimitedError, RetryableError)

    def test_rate_limited_third_party_is_retryable(self) -> None:
        assert issubclass(ThirdPartyRateLimitedError, RetryableError)

    def test_transient_network_is_retryable(self) -> None:
        assert issubclass(TransientNetworkError, RetryableError)

    def test_source_auth_is_non_retryable(self) -> None:
        """SOURCE_AUTH_FAILED is non-retryable — wrong creds stay wrong."""
        assert issubclass(SourceAuthFailedError, NonRetryableError)
        assert not issubclass(SourceAuthFailedError, RetryableError)

    def test_dependency_unavailable_is_non_retryable(self) -> None:
        """DEPENDENCY_UNAVAILABLE is non-retryable — service is down, retry
        won't fix it during this attempt."""
        assert issubclass(DependencyUnavailableError, NonRetryableError)
        assert not issubclass(DependencyUnavailableError, RetryableError)

    def test_unknown_has_default_code(self) -> None:
        """UnknownError must be raisable without app-side code definition,
        because it's the catch-all default."""
        e = UnknownError("nothing fits")
        assert e.category == FailureCategory.UNKNOWN
        assert e.code == "UNKNOWN"
