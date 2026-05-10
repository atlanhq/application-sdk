"""Closed failure category enum and audience enum — the stable vocabulary the SDK owns."""

from enum import Enum


class FailureCategory(Enum):
    """Single-axis failure classification.

    Every value maps to exactly one "what happened" — pick the most specific
    one that applies.  When two categories could apply, use these litmus tests:

    - ``DEPENDENCY_UNAVAILABLE`` vs ``PRECONDITION``: if retrying *the same call*
      without any state change is expected to succeed, use DEPENDENCY_UNAVAILABLE.
      If explicit state must change before the call can succeed, use PRECONDITION.
    - ``RATE_LIMITED`` vs ``RESOURCE_EXHAUSTED``: RATE_LIMITED is a per-key quota
      signal from a remote endpoint (429); RESOURCE_EXHAUSTED is a local resource
      limit (OOM, disk full, file handles).
    - ``ALREADY_EXISTS`` vs ``PRECONDITION``: ALREADY_EXISTS is for idempotent-create
      paths where the entity already exists; PRECONDITION is for broader state
      conflicts where the resource exists but is in the wrong state.
    - ``UNIMPLEMENTED`` vs ``INTERNAL``: UNIMPLEMENTED is a known capability gap
      (feature not built); INTERNAL is an unexpected invariant violation (bug).
    """

    CANCELLED = "CANCELLED"
    """Caller or workflow signalled cancellation; not a failure."""

    TIMEOUT = "TIMEOUT"
    """A bounded wait elapsed (network read, activity start-to-close, heartbeat)."""

    RATE_LIMITED = "RATE_LIMITED"
    """Source or dependency returned 429 or per-key quota signal."""

    AUTH = "AUTH"
    """Credentials missing, expired, or invalid."""

    PERMISSION = "PERMISSION"
    """Authenticated but not authorised for the resource or action."""

    NOT_FOUND = "NOT_FOUND"
    """Targeted entity does not exist."""

    ALREADY_EXISTS = "ALREADY_EXISTS"
    """Entity the caller tried to create already exists (idempotent-create conflict)."""

    INVALID_INPUT = "INVALID_INPUT"
    """Argument or payload malformed irrespective of system state."""

    PRECONDITION = "PRECONDITION"
    """Inputs syntactically valid but system state forbids the action (schema mismatch,
    version conflict, entity in wrong state). Requires explicit state fix before retry;
    use DEPENDENCY_UNAVAILABLE if the same call would succeed on retry."""

    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    """Required platform service down or degraded (Dapr, Temporal, object store, source DB).
    Retrying the same call is expected to succeed; use PRECONDITION if state must change first."""

    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    """Local resource limit hit (OOM, disk full, file handles, worker slots)."""

    DATA_INTEGRITY = "DATA_INTEGRITY"
    """Returned data is corrupt or violates expected invariants."""

    INTERNAL = "INTERNAL"
    """SDK or app bug; invariant broken in our code.  Use ``classification_pending=True``
    on InternalError for caught-but-unclassified failures — they surface as a triage
    backlog rather than hiding among real bugs."""

    UNIMPLEMENTED = "UNIMPLEMENTED"
    """Operation not supported or capability not yet built.  Use instead of INTERNAL
    for known feature gaps so on-call is not paged for expected absence."""

    @property
    def http_status(self) -> int:
        """RFC-standard HTTP status code for this failure category.

        Used by the handler service to map AppError subclasses to HTTP responses
        so callers receive semantically correct status codes without parsing message
        strings.
        """
        _MAP: dict[FailureCategory, int] = {
            FailureCategory.CANCELLED: 499,
            FailureCategory.TIMEOUT: 504,
            FailureCategory.RATE_LIMITED: 429,
            FailureCategory.AUTH: 401,
            FailureCategory.PERMISSION: 403,
            FailureCategory.NOT_FOUND: 404,
            FailureCategory.ALREADY_EXISTS: 409,
            FailureCategory.INVALID_INPUT: 422,
            FailureCategory.PRECONDITION: 409,
            FailureCategory.DEPENDENCY_UNAVAILABLE: 503,
            FailureCategory.RESOURCE_EXHAUSTED: 503,
            FailureCategory.DATA_INTEGRITY: 500,
            FailureCategory.INTERNAL: 500,
            FailureCategory.UNIMPLEMENTED: 501,
        }
        return _MAP[self]


class Audience(Enum):
    """Who needs to take action to resolve this failure.

    Orthogonal to ``FailureCategory`` (what happened) and ``suggested_action``
    (what to do).  Every leaf must pick one of three values — there is no
    UNKNOWN escape hatch.  If you don't know who owns it, the answer is
    APP_OWNER (the team that wrote the code investigates and reclassifies).
    Consumers (AE, SLA dashboards) route on this field without reading
    every leaf code.

    First-responder mapping:
    - USER      → customer self-service / support ticket to the connector owner
    - PLATFORM  → infra ops: check dashboards (Dapr, Temporal, pod health, S3)
    - APP_OWNER → the team that wrote the failing code (connector or SDK):
                  file a bug, add a feature, or add a more specific subclass
                  to migrate this case out of the catch-all bucket
    """

    USER = "USER"
    """The end-user (connector owner) must act — credentials, permissions,
    source config, or a resource they control."""

    PLATFORM = "PLATFORM"
    """Infra ops must act — a shared service or infrastructure component is
    unavailable or exhausted (Dapr, Temporal, object store, pod OOM)."""

    APP_OWNER = "APP_OWNER"
    """The app team that owns the failing code must act — a connector bug,
    an SDK bug, an unimplemented capability, a data-integrity failure, or
    a caught-but-unclassified failure pending triage. From production
    routing's standpoint, SDK and connector code share an owner: the team
    debugs first, escalates to the SDK team if needed."""
