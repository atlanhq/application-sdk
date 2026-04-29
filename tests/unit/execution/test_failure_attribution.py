"""Unit tests for failure attribution surfacing through ApplicationError.details.

Tests the helper that builds the structured-fields payload from a typed
exception. The activity wrapper at activities.py uses this to populate
ApplicationError.details so consumers (Automation Engine, dashboards,
OTel pipelines) can read structured failure attribution without parsing
the exception message string.
"""

from __future__ import annotations

from application_sdk.app.base import NonRetryableError
from application_sdk.execution._temporal.activities import (
    _build_classified_failure_details,
)


# ---------------------------------------------------------------------------
# Test fixtures: example typed exceptions following the convention
# ---------------------------------------------------------------------------


class _CustomerInfraRedshiftAuth(NonRetryableError):
    """Example app-level typed exception following the failure-attribution
    convention (owner / sub_category / code / customer_message)."""

    owner = "customer"
    sub_category = "credentials"
    code = "CUSTOMER_INFRA_REDSHIFT_AUTH_FAILED"
    customer_message = (
        "Authentication to your Redshift cluster failed. "
        "Please verify the credentials configured in Atlan."
    )

    def __init__(
        self,
        internal_message: str,
        evidence: dict | None = None,
        cause: BaseException | None = None,
    ):
        self.internal_message = internal_message
        self.evidence = evidence or {}
        # Construct the exception with the message; __cause__ is set by
        # `raise X from cause` at the call site.
        super().__init__(internal_message)


class _PlainNonRetryable(NonRetryableError):
    """An existing-style NonRetryableError that does NOT follow the
    convention. Backwards-compat case: should produce an empty details list."""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBuildClassifiedFailureDetails:
    def test_typed_exception_emits_structured_payload(self) -> None:
        try:
            raise _CustomerInfraRedshiftAuth(
                internal_message="FATAL: password authentication failed for user atlan",
                evidence={"host": "warehouse.example.com", "pg_error": "28P01"},
            )
        except _CustomerInfraRedshiftAuth as exc:
            details = _build_classified_failure_details(exc)

        assert len(details) == 1
        payload = details[0]

        # The convention fields ride along on the class:
        assert payload["owner"] == "customer"
        assert payload["sub_category"] == "credentials"
        assert payload["code"] == "CUSTOMER_INFRA_REDSHIFT_AUTH_FAILED"
        assert payload["customer_message"].startswith("Authentication to your")

        # Instance fields set at construction:
        assert payload["internal_message"].startswith("FATAL: password")
        assert payload["evidence"] == {
            "host": "warehouse.example.com",
            "pg_error": "28P01",
        }

        # Source/tenant come from APPLICATION_NAME / APP_TENANT_ID env-driven
        # constants. They're populated regardless (default to "default").
        assert "source_app" in payload
        assert "tenant_id" in payload

    def test_cause_chain_preserved(self) -> None:
        """When raised with `from cause`, the cause string is captured."""
        original = ValueError("upstream parse failure")
        try:
            try:
                raise original
            except ValueError as v:
                raise _CustomerInfraRedshiftAuth(
                    internal_message="wrapped failure",
                ) from v
        except _CustomerInfraRedshiftAuth as exc:
            details = _build_classified_failure_details(exc)

        assert len(details) == 1
        assert details[0]["cause"] is not None
        assert "upstream parse failure" in details[0]["cause"]

    def test_no_cause_yields_none(self) -> None:
        try:
            raise _CustomerInfraRedshiftAuth(internal_message="standalone failure")
        except _CustomerInfraRedshiftAuth as exc:
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
        """Even non-NonRetryableError exceptions produce no payload — the
        helper is purely about the convention attributes, not the class."""
        try:
            raise ValueError("plain old value error")
        except ValueError as exc:
            details = _build_classified_failure_details(exc)

        assert details == []

    def test_evidence_defaults_to_empty_dict(self) -> None:
        """evidence is optional — class without it surfaces empty dict."""

        class _NoEvidence(NonRetryableError):
            owner = "platform"
            sub_category = "bug"
            code = "PLATFORM_TEST_NO_EVIDENCE"

            def __init__(self, msg: str):
                self.internal_message = msg
                super().__init__(msg)

        try:
            raise _NoEvidence("oops")
        except _NoEvidence as exc:
            details = _build_classified_failure_details(exc)

        assert details[0]["evidence"] == {}

    def test_customer_message_optional(self) -> None:
        """customer_message is allowed to be None for internal-only failures."""

        class _Internal(NonRetryableError):
            owner = "platform"
            sub_category = "infrastructure"
            code = "PLATFORM_INTERNAL_TEST"
            customer_message = None

            def __init__(self, msg: str):
                self.internal_message = msg
                super().__init__(msg)

        try:
            raise _Internal("our problem")
        except _Internal as exc:
            details = _build_classified_failure_details(exc)

        assert details[0]["customer_message"] is None
