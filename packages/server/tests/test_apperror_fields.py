"""`retryable=` and `cause=` have to take effect, not land in evidence.

Both are real fields on application_sdk's AppError, so
``AuthError("x", retryable=True, cause=exc)`` is valid Python in both packages.
Here they used to fall into **context: the wire said ``retryable: false`` while
the caller had asked for true, and a retryable source failure was reported as
terminal. No error, no warning -- just the string "True" sitting in evidence.
"""

from __future__ import annotations

import warnings

import pytest
from server_sdk.errors.base import AppError, HandlerError
from server_sdk.errors.leaves import AuthError, SourceUnavailableError
from server_sdk.handler.contracts import PreflightCheck


def test_retryable_kwarg_reaches_the_wire() -> None:
    assert AuthError("x", retryable=True).to_failure_details().retryable is True


def test_a_leaf_keeps_its_own_default() -> None:
    """The base must not be renamed to `default_retryable`: five leaves spell it
    `retryable = True`, and renaming only the base silently un-retries them."""
    assert SourceUnavailableError("x").to_failure_details().retryable is True


def test_a_leaf_default_can_still_be_overridden() -> None:
    assert (
        SourceUnavailableError("x", retryable=False).to_failure_details().retryable
        is False
    )


def test_cause_is_chained_and_capped_on_the_wire() -> None:
    cause = ValueError("boom")
    err = AuthError("x", cause=cause)
    assert err.__cause__ is cause
    assert err.to_failure_details().cause_repr == "ValueError: boom"


def test_identity_kwargs_are_fields_not_evidence() -> None:
    fd = AuthError("x", app_name="acme", run_id="r1").to_failure_details()
    assert (fd.app_name, fd.run_id) == ("acme", "r1")
    assert fd.evidence == {}


def test_genuine_context_still_becomes_evidence() -> None:
    fd = AuthError("x", auth_method="oauth").to_failure_details()
    assert fd.evidence == {"auth_method": "oauth"}


def test_suggested_action_is_promoted_not_duplicated() -> None:
    fd = AppError("x", suggested_action="retry").to_failure_details()
    assert fd.suggested_action == "retry"
    assert "suggested_action" not in fd.evidence


# ── the unmeasured-duration sentinel ────────────────────────────────────────


def test_duration_ms_defaults_to_the_unmeasured_sentinel() -> None:
    """0.0 would make an untimed check indistinguishable from an instant one,
    biasing every p50/p99 over check duration toward zero."""
    assert PreflightCheck(name="c").duration_ms == -1.0


# ── HandlerError is not part of the new public surface ──────────────────────


def test_handler_error_is_not_advertised() -> None:
    import server_sdk
    import server_sdk.errors

    assert "HandlerError" not in server_sdk.__all__
    assert "HandlerError" not in server_sdk.errors.__all__


def test_handler_error_still_imports_and_catches() -> None:
    """The three `except HandlerError` sites and any app already importing it
    must keep working."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        err = HandlerError("x", http_status=418)
    assert err.http_status == 418
    assert isinstance(err, AppError)


def test_constructing_handler_error_warns() -> None:
    with pytest.warns(DeprecationWarning, match="removed in v4.0"):
        HandlerError("x")


# ── a connector subclass may already own `cause` ────────────────────────────


def test_a_subclass_may_expose_cause_as_its_own_property() -> None:
    """redshift_server's error base does exactly this.

    A bare ``self.cause = ...`` in AppError.__init__ raises
    "property 'cause' has no setter" against such a subclass, which breaks
    every error that app constructs -- 52 of redshift's server tests at once.
    """

    class ConnectorError(AppError):
        def __init__(self, message="", *, cause=None, **kw):
            super().__init__(message, **kw)
            self._cause = cause
            if cause is not None and self.__cause__ is None:
                self.__cause__ = cause

        @property
        def cause(self):  # read-only, shadows the base
            return self._cause

    exc = ValueError("boom")
    err = ConnectorError("nope", cause=exc)
    assert err.cause is exc
    assert err.__cause__ is exc
    # And the base still routes cause_repr through the override.
    assert err.to_failure_details().cause_repr == "ValueError: boom"


def test_the_base_still_populates_cause_when_not_overridden() -> None:
    assert AuthError("x", cause=ValueError("b")).cause is not None
