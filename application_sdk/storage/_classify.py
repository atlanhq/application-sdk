"""The storage package's failure classifiers, in one module.

:mod:`application_sdk._storage_classify` holds the obstore-free half that the
worker and the consolidated API host share: what a failed request *is*
(missing object, missing container, a gateway that has not loaded its routes
or could not verify the request). This module holds the half only the worker
needs, which may name obstore's exception classes directly:

* :func:`classify_access_error`, the preflight probe's bucket and remediation
  hint for a failed access check, which the write path also reads to tell a
  bucket relocation from any other failure.
* :func:`is_precondition`, an etag precondition miss on a version-pinned GET.
* :func:`denied_evidence`, the ``(http_status, provider_code)`` of a 401/403
  anywhere on a failure's cause chain.

It imports nothing from its storage siblings, so ``ops``, ``preflight`` and
``cloud`` all import it at module load with no cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from obstore.exceptions import (
    PermissionDeniedError,
    PreconditionError,
    UnauthenticatedError,
)

from application_sdk import _storage_classify

# Connectivity/unknown hint — shared between the classifier's fallback bucket
# and the preflight timeout branch so a future classifier change cannot
# silently diverge.
_CONNECTIVITY_HINT = (
    "Could not reach the object store. Check the endpoint URL, "
    "bucket/container name, and network connectivity from this pod/host."
)

# Bucket for a binding that was never probed because it is absent or
# unparseable.  Bypasses the classifier entirely (there is no exception to
# classify), so it is named here rather than derived from a rule.
_NOT_CONFIGURED = "not configured"
_CONNECTIVITY_UNKNOWN = "connectivity / unknown"

# Pre-compiled patterns for HTTP status codes.  Word-boundary anchors prevent
# false positives from request IDs, byte counts, or longer strings that happen
# to contain "401" / "403" as a substring.
_RE_403 = re.compile(r"\b403\b")
_RE_401 = re.compile(r"\b401\b")


@dataclass(frozen=True)
class _AccessErrorRule:
    """One classifier bucket: how to recognise it, and what to tell the reader.

    Table-driven rather than a chain of ``if`` branches so the set of buckets
    the probe can emit is *enumerable* — see :data:`_OBJECT_STORE_ERROR_CLASSES`.
    A new bucket added as a branch could fall through the consumer's
    bucket-to-error-leaf mapping unnoticed; a new bucket added as a rule cannot.
    """

    bucket: str
    hint: str
    pattern: re.Pattern[str] | None = None
    markers: tuple[str, ...] = ()

    def matches(self, msg: str) -> bool:
        """Whether *msg* (already lowercased) falls in this bucket."""
        if self.pattern is not None and self.pattern.search(msg):
            return True
        return any(marker in msg for marker in self.markers)


# The relocation bucket's name, exported so every consumer that keys behavior
# off this bucket (the preflight gate's code stamp, the upload path's typed
# error) imports it from here instead of copying the literal — renaming the
# bucket then breaks loudly at import/test time rather than silently dropping
# the consumers' special-casing.
RELOCATION_BUCKET = "bucket relocation in progress"

_ACCESS_ERROR_RULES: tuple[_AccessErrorRule, ...] = (
    # First: the most specific bucket. Requires BOTH tokens so an etag/if-match
    # 412 ("precondition" alone) is never misread as a relocation. GCS rejects
    # multipart *initiation* with HTTP 400 PreconditionFailed and a message
    # naming the relocation for the entire dual-/multi-region move window,
    # while plain single-request PUTs keep working — confirmed in a production
    # RCA where small artifacts uploaded fine and only multipart ones failed.
    # The matched shape is the native GCSStore error; behind the s3proxy
    # compose-based multipart emulation the message shape is unverified — an
    # unrecognized rejection still fails the probe, classified
    # connectivity/unknown.
    _AccessErrorRule(
        bucket=RELOCATION_BUCKET,
        pattern=re.compile(r"precondition.*relocation|relocation.*precondition", re.S),
        hint=(
            "The bucket is being relocated (dual-/multi-region move); the store "
            "rejects multipart upload initiation until the relocation completes. "
            "This is a temporary, platform-side condition — no credential or "
            "permission change will fix it. Retry after the relocation finishes."
        ),
    ),
    _AccessErrorRule(
        bucket="permission denied",
        pattern=_RE_403,
        markers=(
            "accessdenied",
            "access denied",
            "forbidden",
            "not authorized",
            "authorization failed",
        ),
        hint=(
            "The credentials are valid but lack the required read/write "
            "permissions on this bucket. Grant the IAM/ACL permissions needed "
            "for get and put operations."
        ),
    ),
    _AccessErrorRule(
        bucket="invalid credentials",
        pattern=_RE_401,
        markers=(
            "invalidaccesskeyid",
            "invalid access key",
            "signaturedoesnotmatch",
            "unauthenticated",
            "invalid credentials",
        ),
        hint=(
            "The credentials in the Dapr component appear to be invalid "
            "(wrong key, expired token, or malformed secret). Update the binding "
            "component or the referenced secret values."
        ),
    ),
)

# Bucket used when no rule matches — a network fault, an unmapped provider
# error, or a probe timeout.
_FALLBACK_RULE = _AccessErrorRule(
    bucket=_CONNECTIVITY_UNKNOWN,
    hint=_CONNECTIVITY_HINT,
)

# Every ``error_class`` a failed :class:`ObjectStoreCheckResult` can carry: the
# classifier's rules, its fallback, and the not-configured path that bypasses
# it.  Consumers mapping buckets onto typed errors assert against *this* rather
# than a copy of the literals, so adding a rule without a mapping fails a test
# instead of silently taking the consumer's default branch.
_OBJECT_STORE_ERROR_CLASSES: frozenset[str] = frozenset(
    {rule.bucket for rule in (*_ACCESS_ERROR_RULES, _FALLBACK_RULE)} | {_NOT_CONFIGURED}
)


def classify_access_error(exc: BaseException) -> tuple[str, str]:
    """Classify an obstore exception into (error_class, remediation_hint).

    Matches the lowercased error message against :data:`_ACCESS_ERROR_RULES`.
    HTTP status codes use word-boundary regex (``\\b403\\b``) to avoid false
    positives from request IDs or byte counts that happen to contain those
    digits.

    Every bucket returned here is in :data:`_OBJECT_STORE_ERROR_CLASSES` by
    construction — both are derived from :data:`_ACCESS_ERROR_RULES`.

    Returns:
        A 2-tuple of (error_class_label, remediation_hint).
    """
    msg = str(exc).lower()

    for rule in _ACCESS_ERROR_RULES:
        if rule.matches(msg):
            return (rule.bucket, rule.hint)

    return (_FALLBACK_RULE.bucket, _FALLBACK_RULE.hint)


def is_precondition(exc: BaseException) -> bool:
    """True when *exc* is an etag precondition failure (HTTP 412 / if_match miss).

    Raised by version-pinned range GETs when the remote object was rewritten
    between the initial HEAD/listing and the chunk fetch. obstore types it as
    :class:`~obstore.exceptions.PreconditionError`; a generic error is matched
    on its parsed HTTP status, never on the digits ``412`` anywhere in its
    text, because object keys and run GUIDs appear in every obstore message
    and roughly one artifact key in 94 contains them.
    """
    if isinstance(exc, PreconditionError):
        return True
    http_status, _ = _storage_classify.http_evidence(exc)
    return http_status == 412


# Statuses that mean the caller's credential or the bucket's policy refused the
# request. obstore maps some of these to a typed error and leaves the rest as
# text, so both are checked.
_DENIED_STATUSES = frozenset({401, 403})
_DENIED_ERROR_STATUS: tuple[tuple[type[Exception], int], ...] = (
    (PermissionDeniedError, 403),
    (UnauthenticatedError, 401),
)


def denied_evidence(exc: BaseException) -> tuple[int, str | None] | None:
    """Return ``(http_status, provider_code)`` when *exc* is a 401/403.

    Walks explicit ``__cause__`` links so a ``StorageError`` the shared
    primitives already wrapped is judged by the driver error inside it.
    """
    current: BaseException | None = exc
    while current is not None:
        http_status, provider_code = _storage_classify.http_evidence(current)
        if http_status is None:
            http_status = next(
                (s for t, s in _DENIED_ERROR_STATUS if isinstance(current, t)), None
            )
        if http_status in _DENIED_STATUSES:
            return http_status, provider_code
        current = current.__cause__
    return None
