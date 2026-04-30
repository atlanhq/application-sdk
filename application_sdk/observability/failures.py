"""Failure attribution — closed taxonomy + typed base classes.

The SDK owns the **category** vocabulary. Apps own the leaf classes and
specific codes. App teams pick a category by inheriting the right base
class — they cannot invent new categories.

Design principles
-----------------

1. **Source-side classification.** The function that raised the error has
   the context. Surface the classification at the throw site, not by
   parsing a string downstream.

2. **One axis (category), not two (owner + sub_category).** Direction of
   the failure is encoded in the category prefix; the routing layer reads
   one field. No redundant ``owner`` field.

3. **Connector-agnostic categories.** The category names do not mention
   Redshift, Snowflake, Athena, etc. Connector identity rides in
   ``source_app`` and ``evidence``. This keeps the central taxonomy stable
   as new connectors land.

4. **App vocabulary stays free.** ``code`` and ``customer_message`` are
   set by the app on its leaf class. The SDK has no opinion about what
   codes look like beyond convention.

5. **Backwards compatible.** Existing ``NonRetryableError`` subclasses
   without category attributes are still serialized correctly into
   ``ApplicationError.details`` — the helper returns an empty list when
   the convention attributes are missing.

Wire format
-----------

When a typed exception bubbles up through the SDK activity wrapper, the
wrapper serializes its class attributes into ``ApplicationError.details``::

    {
        "category":         "DEPENDENCY_UNAVAILABLE",
        "code":             "METASTORE_UNAVAILABLE",
        "source_app":       "publish",
        "tenant_id":        "markeznp29",
        "internal_message": "httpx.ConnectError: ...",
        "customer_message": None,
        "evidence":         {"host": "atlas.atlan.io", "http_status": 503},
        "cause":            "ConnectError(...)"
    }

The Automation Engine and dashboards read these structured fields directly
— no string parsing.

Concrete usage
--------------

In an app::

    from application_sdk.observability.failures import (
        SourceAuthFailedError,
        DependencyUnavailableError,
    )

    class RedshiftAuthFailure(SourceAuthFailedError):
        code = "REDSHIFT_AUTH_FAILED"
        customer_message = (
            "Authentication to your Redshift cluster failed. "
            "Please verify the credentials configured in Atlan."
        )

    class MetastoreUnavailable(DependencyUnavailableError):
        code = "METASTORE_UNAVAILABLE"
        customer_message = None  # internal — engineering on-call

At the throw site::

    raise RedshiftAuthFailure(
        internal_message=str(e),
        evidence={"host": host, "pg_error": e.pgcode},
        cause=e,
    )

Catching::

    try:
        fetch_metadata()
    except SourceError:
        # Anything customer-source-side, regardless of connector.
        ...
    except DependencyError:
        # Anything from a shared internal dependency.
        ...
    except NonRetryableError:
        # Existing SDK behavior — preserved.
        ...
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from application_sdk.app.base import NonRetryableError, RetryableError


class FailureCategory(StrEnum):
    """Closed taxonomy of failure categories.

    Apps cannot extend this enum. Adding a new category requires an SDK PR
    reviewed by the schema owner. App-specific failure modes are expressed
    via the ``code`` attribute on the leaf class, not by inventing new
    categories.

    Direction (who is responsible) is encoded in the prefix, so routing
    layers read this single field rather than a separate ``owner``:

      - ``SOURCE_*``        customer's source (their cluster, their network,
                            their data, their config in Atlan)
      - ``DEPENDENCY_*``    shared internal dependency we own
                            (metastore, our S3, our OAuth provider)
      - ``THIRD_PARTY_*``   external vendor we depend on
                            (AWS control plane, Snowflake API)
      - ``PLATFORM_*``      our code or our infra
      - ``UPSTREAM_FAILED`` previous Atlan stage produced bad output we
                            cannot consume (Publish reading bad crawler
                            output, QI reading bad miner output)
      - ``TRANSIENT_*``     recoverable network-class blip
      - ``UNKNOWN``         unclassified — HIGH severity, forces triage
    """

    # Customer's source side — they are responsible for fixing
    SOURCE_AUTH_FAILED = "SOURCE_AUTH_FAILED"
    SOURCE_PERMISSION_DENIED = "SOURCE_PERMISSION_DENIED"
    SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
    SOURCE_CONFIG_INVALID = "SOURCE_CONFIG_INVALID"
    SOURCE_DATA_INVALID = "SOURCE_DATA_INVALID"
    SOURCE_LIMIT_EXCEEDED = "SOURCE_LIMIT_EXCEEDED"

    # Internal shared dependency — we own the dep
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DEPENDENCY_RATE_LIMITED = "DEPENDENCY_RATE_LIMITED"

    # External vendor — neither customer nor us
    THIRD_PARTY_UNAVAILABLE = "THIRD_PARTY_UNAVAILABLE"
    THIRD_PARTY_RATE_LIMITED = "THIRD_PARTY_RATE_LIMITED"

    # Our problem
    PLATFORM_BUG = "PLATFORM_BUG"
    PLATFORM_INFRA = "PLATFORM_INFRA"

    # Cross-stage data-flow
    UPSTREAM_FAILED = "UPSTREAM_FAILED"

    # Recoverable
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"

    # Default for caught-but-unclassified
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Mixin: provides the classified-failure contract (instance fields + class
# attributes the SDK activity wrapper reads).
# ---------------------------------------------------------------------------


class _ClassifiedFailureMixin:
    """Pure mixin: holds the structured-field contract.

    Concrete leaves set ``category``, ``code`` (and optionally
    ``customer_message``) at the class level. Instance fields
    (``internal_message``, ``evidence``) are passed at construction.

    Do not inherit from this directly. Inherit from one of the typed base
    classes below (``SourceAuthFailedError``, ``DependencyUnavailableError``,
    etc.), which set ``category`` for you.
    """

    category: ClassVar[FailureCategory]
    code: ClassVar[str]
    customer_message: ClassVar[str | None] = None

    def __init__(
        self,
        internal_message: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.internal_message = internal_message
        self.evidence = evidence or {}
        super().__init__(internal_message)


# ---------------------------------------------------------------------------
# Marker classes — group bases by direction so apps can `except` broadly.
#
# These are pure markers (no state). They sit between the retry roots and
# the per-category bases so that downstream code can write::
#
#     except SourceError:        # any customer-source failure
#     except DependencyError:    # any shared-dep failure
#     except ThirdPartyError:    # any vendor failure
#     except PlatformError:      # any of-our-doing failure
# ---------------------------------------------------------------------------


class SourceError(_ClassifiedFailureMixin, NonRetryableError):
    """Base for failures originating in the customer's source.

    Includes auth, permissions, network reachability, configuration,
    data shape, and limits hit on the customer side.
    """


class DependencyError(_ClassifiedFailureMixin, NonRetryableError):
    """Base for failures in a shared internal dependency.

    Things we operate that other apps depend on: the metastore (Atlas),
    our S3 buckets, our OAuth provider, our state stores. Two leaves so
    far — unavailable (5xx) and rate-limited (429-class).
    """


class ThirdPartyError(_ClassifiedFailureMixin, NonRetryableError):
    """Base for failures in an external vendor we depend on.

    AWS control plane (STS, Glue, Athena service-side), Snowflake API,
    customer's data source provider's own SaaS API. Distinct from
    ``SourceError`` (customer's data) and ``DependencyError`` (our infra).
    """


class PlatformError(_ClassifiedFailureMixin, NonRetryableError):
    """Base for failures originating in our own code or our own infra.

    PLATFORM_BUG = code defect, uncaught exception, state corruption.
    PLATFORM_INFRA = our worker OOM, our pod evicted, our DB connection dead.
    """


class UpstreamError(_ClassifiedFailureMixin, NonRetryableError):
    """Base for failures caused by a previous Atlan stage's bad output.

    Publish reading malformed Parquet from the crawler stage. QI reading
    incomplete miner output. The current stage refuses to process; the
    triage points to the previous stage's run via ``cause`` or evidence.
    """


class TransientFailure(_ClassifiedFailureMixin, RetryableError):
    """Base for retryable network-class failures.

    Temporal will retry per the activity's retry policy. Use sparingly —
    only for failures that genuinely have a chance of recovering.
    """


class UnknownError(_ClassifiedFailureMixin, NonRetryableError):
    """Default for caught-but-unclassified exceptions.

    HIGH severity — anything that lands here means the taxonomy didn't
    match. Triage decides whether it's a new pattern that deserves an
    explicit handler.
    """

    category = FailureCategory.UNKNOWN
    code = "UNKNOWN"


# ---------------------------------------------------------------------------
# Per-category bases — apps subclass these. The category is set here once;
# leaf classes only need to set ``code`` and (optionally) ``customer_message``.
# ---------------------------------------------------------------------------


# SOURCE_* — customer is responsible

class SourceAuthFailedError(SourceError):
    """Customer's source rejected our authentication.

    Bad credentials, expired token, invalid client_id/secret. The
    customer's fix is "update credentials in Atlan."
    """

    category = FailureCategory.SOURCE_AUTH_FAILED


class SourcePermissionDeniedError(SourceError):
    """Authenticated, but the principal lacks required permissions.

    Distinct from ``SourceAuthFailedError`` because the fix is different —
    the customer needs to update IAM policy or grant SQL permissions, not
    rotate credentials.
    """

    category = FailureCategory.SOURCE_PERMISSION_DENIED


class SourceUnreachableError(SourceError):
    """Customer's source cannot be reached.

    DNS resolution, TCP connect refused/timeout, TLS handshake, regional
    misconfiguration. Customer's network or hostname problem.
    """

    category = FailureCategory.SOURCE_UNREACHABLE


class SourceConfigInvalidError(SourceError):
    """Customer's configuration in Atlan is rejected.

    Malformed host, missing required field (cluster_id, workgroup),
    nonexistent database/schema name, invalid region.
    """

    category = FailureCategory.SOURCE_CONFIG_INVALID


class SourceDataInvalidError(SourceError):
    """Customer's data shape we cannot process.

    Malformed parquet, unsupported column type, schema mismatch, parser
    failure on customer's SQL, circular references in customer metadata.
    The customer's data is the cause; the failure surfaces in our parser
    or transformer.
    """

    category = FailureCategory.SOURCE_DATA_INVALID


class SourceLimitExceededError(SourceError):
    """Resource/safety limit tripped by customer-side cause.

    Circuit breaker on volume (87% deletes), query timeout caused by
    customer's slow cluster, customer concurrent connection limit. Distinct
    from ``SourceDataInvalidError`` because the *volume* is the signal,
    not a malformed shape.
    """

    category = FailureCategory.SOURCE_LIMIT_EXCEEDED


# DEPENDENCY_* — our internal dependency

class DependencyUnavailableError(DependencyError):
    """Shared internal dependency is down (metastore, our S3, our OAuth)."""

    category = FailureCategory.DEPENDENCY_UNAVAILABLE


class DependencyRateLimitedError(DependencyError, RetryableError):
    """Shared internal dependency throttling us. Retryable.

    Multi-inherits ``RetryableError`` because it's the only ``Dependency*``
    that should retry. Temporal's retry policy applies; backoff respects
    Retry-After if present in evidence.
    """

    category = FailureCategory.DEPENDENCY_RATE_LIMITED


# THIRD_PARTY_* — external vendor

class ThirdPartyUnavailableError(ThirdPartyError):
    """External vendor outage (AWS control plane, Snowflake API)."""

    category = FailureCategory.THIRD_PARTY_UNAVAILABLE


class ThirdPartyRateLimitedError(ThirdPartyError, RetryableError):
    """External vendor throttling us. Retryable.

    Same retry-friendly multi-inheritance pattern as
    ``DependencyRateLimitedError``.
    """

    category = FailureCategory.THIRD_PARTY_RATE_LIMITED


# PLATFORM_* — our problem

class PlatformBugError(PlatformError):
    """Our code defect — uncaught exception, state corruption, bad assertion.

    Default classification for anything caught by a defensive wrapper that
    doesn't match an explicit handler. Engineering investigates; if the
    pattern recurs and turns out to be customer-caused, an explicit
    ``Source*`` handler is added in a follow-up.
    """

    category = FailureCategory.PLATFORM_BUG


class PlatformInfraError(PlatformError):
    """Our infrastructure problem.

    Worker OOM, pod evicted, our DB connection dead, our deployment is
    misconfigured. Distinct from ``PlatformBugError`` because the fix is
    operational rather than a code change.
    """

    category = FailureCategory.PLATFORM_INFRA


# UPSTREAM — cross-stage

class UpstreamFailedError(UpstreamError):
    """Previous Atlan stage produced output this stage cannot consume.

    Publish reading malformed Parquet from a crawler. QI reading
    incomplete miner output. Triage chases the previous stage's run.
    """

    category = FailureCategory.UPSTREAM_FAILED


# TRANSIENT — retryable

class TransientNetworkError(TransientFailure):
    """Recoverable network-class blip (brief disconnect, momentary timeout)."""

    category = FailureCategory.TRANSIENT_NETWORK
