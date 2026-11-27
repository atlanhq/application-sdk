"""Unit tests for :mod:`application_sdk.storage._classify`.

The access classifier's buckets (moved here from ``test_preflight.py`` with the
classifier), the etag-precondition rule chunked downloads restart on, and the
401/403 evidence ``CloudStore`` turns into a typed USER error.
"""

from __future__ import annotations

import pytest
from obstore.exceptions import (
    PermissionDeniedError,
    PreconditionError,
    UnauthenticatedError,
)

from application_sdk.storage import _classify, ops, preflight
from application_sdk.storage._classify import (
    classify_access_error as _classify_access_error,
)
from application_sdk.storage._classify import denied_evidence, is_precondition
from application_sdk.storage.errors import StorageError

_RELOCATION_MESSAGE = (
    "Generic GCS error: Server returned non-2xx status code: 400 Bad Request: "
    "<Error><Code>PreconditionFailed</Code><Message>Invalid precondition during "
    "a custom dual-region, or multi-region bucket relocation.</Message></Error>"
)

# ---------------------------------------------------------------------------
# _classify_access_error — parametrised
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,expected_class",
    [
        # 403 HTTP status — word-boundary match
        ("HTTP 403 Forbidden", "permission denied"),
        # Ensure bare "403" in isolation still matches
        ("status code 403", "permission denied"),
        # Must NOT match "2403" or "4030" (false positive guard)
        ("request-id x-amz-request-id: 1234030", "connectivity / unknown"),
        # Named strings — 403 bucket
        ("AccessDenied: Access Denied", "permission denied"),
        ("access denied to resource", "permission denied"),
        ("Forbidden resource", "permission denied"),
        ("not authorized to perform this action", "permission denied"),
        ("authorization failed", "permission denied"),
        # 401 HTTP status — word-boundary match
        ("HTTP 401 Unauthorized", "invalid credentials"),
        # Ensure bare "401" in isolation still matches
        ("error code 401", "invalid credentials"),
        # Must NOT match "1401" or "4010" (false positive guard)
        ("byte count 14010", "connectivity / unknown"),
        # Named strings — 401 bucket
        (
            "InvalidAccessKeyId: The AWS Access Key Id you provided does not exist",
            "invalid credentials",
        ),
        ("invalid access key supplied", "invalid credentials"),
        ("SignatureDoesNotMatch: signature mismatch", "invalid credentials"),
        ("unauthenticated request", "invalid credentials"),
        ("invalid credentials provided", "invalid credentials"),
        # Connectivity fallback
        ("connection refused", "connectivity / unknown"),
        ("no route to host", "connectivity / unknown"),
        ("bucket does not exist", "connectivity / unknown"),
        ("name or service not known", "connectivity / unknown"),
    ],
)
def test_classify_access_error(message: str, expected_class: str) -> None:
    exc = Exception(message)
    error_class, hint = _classify_access_error(exc)
    assert error_class == expected_class, (
        f"For message={message!r}: expected {expected_class!r} but got {error_class!r}"
    )
    assert isinstance(hint, str) and len(hint) > 0


def test_classify_access_error_returns_hint_for_each_class() -> None:
    """All three classifiers return a non-empty hint."""
    for msg, expected in [
        ("403 Forbidden", "permission denied"),
        ("401 Unauthorized", "invalid credentials"),
        ("timeout", "connectivity / unknown"),
    ]:
        _, hint = _classify_access_error(Exception(msg))
        assert hint, f"Empty hint for class={expected!r}"


@pytest.mark.parametrize(
    "message,expected_class",
    [
        # The production shape: both tokens present.
        (_RELOCATION_MESSAGE, "bucket relocation in progress"),
        # Reversed token order still matches.
        (
            "bucket relocation rejected the request: precondition failed",
            "bucket relocation in progress",
        ),
        # An etag/if-match 412 carries "precondition" alone → NOT relocation.
        (
            "412 Precondition Failed: at-match condition not met",
            "connectivity / unknown",
        ),
        # "relocation" alone (no precondition) → NOT relocation.
        ("object relocation pending", "connectivity / unknown"),
    ],
)
def test_classify_relocation(message: str, expected_class: str) -> None:
    error_class, hint = _classify_access_error(Exception(message))
    assert error_class == expected_class
    assert hint


# ---------------------------------------------------------------------------
# is_precondition
# ---------------------------------------------------------------------------


def test_is_precondition_matches_obstores_typed_error() -> None:
    assert is_precondition(PreconditionError("if-match condition not met"))


def test_is_precondition_matches_a_generic_412_by_its_parsed_status() -> None:
    exc = Exception(
        "Generic S3 error: Error performing GET https://h/b/k in 1ms - "
        "Server returned non-2xx status code: 412 Precondition Failed: "
    )
    assert is_precondition(exc)


def test_is_precondition_ignores_412_inside_a_key() -> None:
    """A run GUID containing ``412`` on a 503 must not restart the download."""
    key = "artifacts/apps/example-app/runs/01a0412f-5c0c-7195/t.json"
    exc = Exception(
        f"Generic GCS error: Error performing GET https://h/{key} in 1ms - "
        "Server returned non-2xx status code: 503 Service Unavailable: "
    )
    assert not is_precondition(exc)


# ---------------------------------------------------------------------------
# denied_evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (PermissionDeniedError("nope"), (403, None)),
        (UnauthenticatedError("nope"), (401, None)),
        (
            Exception(
                "Client error with status 403 Forbidden: "
                "<Error><Code>AccessDenied</Code></Error>"
            ),
            (403, "AccessDenied"),
        ),
        (Exception("Server returned non-2xx status code: 503 Unavailable"), None),
        (Exception("connection refused"), None),
    ],
)
def test_denied_evidence(exc: BaseException, expected: object) -> None:
    assert denied_evidence(exc) == expected


def test_denied_evidence_walks_the_cause_chain() -> None:
    """A ``StorageError`` the primitives wrapped is judged by its driver error."""
    driver = PermissionDeniedError("Client error with status 403 Forbidden: ")
    wrapped = StorageError("Failed to get key 'k'", key="k", cause=driver)
    wrapped.__cause__ = driver
    assert denied_evidence(wrapped) == (403, None)


# ---------------------------------------------------------------------------
# One definition, read from every old import site
# ---------------------------------------------------------------------------


def test_old_import_sites_bind_the_classifier_module_objects() -> None:
    """``preflight`` and ``ops`` re-bind, they do not re-define.

    A copy at either site would let the write path and the preflight probe
    drift apart on what a relocation is.
    """
    assert preflight._classify_access_error is _classify.classify_access_error
    assert preflight.RELOCATION_BUCKET is _classify.RELOCATION_BUCKET
    assert (
        preflight._OBJECT_STORE_ERROR_CLASSES is _classify._OBJECT_STORE_ERROR_CLASSES
    )
    assert ops._ObstorePreconditionError is PreconditionError
