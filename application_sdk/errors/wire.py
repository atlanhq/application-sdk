"""FailureDetails — Pydantic wire envelope carried in ApplicationError.details.

Both directions of the envelope live here: :class:`FailureDetails` is what an
``AppError`` serialises to, and :func:`error_from_failure_details` is the
reverse lookup that rebuilds a typed error from one on the far side of an
activity boundary, where only the envelope survives. The reverse lookup keys on
``code`` through an explicit leaf registry (:func:`register_wire_leaf`).
"""

from __future__ import annotations

import dataclasses
import inspect
import threading
from collections.abc import Callable, Mapping
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from application_sdk.errors.base import (
    AppError,
    redact_secrets,
    redact_wire_value,
    sanitize_cause_repr,
)
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AppTimeoutError,
    AuthError,
    InternalError,
    SourceUnavailableError,
)

# Keys that may carry secrets — rejected at envelope construction.
_EVIDENCE_KEY_DENYLIST: frozenset[str] = frozenset(
    {
        "auth_header",
        "authorization",
        "cookie",
        "token",
        "password",
        "secret",
        "api_key",
        "private_key",
    }
)

# Compound variants like ``client_secret`` or ``db_password``, matched by
# suffix so generic names such as ``object_key`` or ``cache_key`` still pass.
_EVIDENCE_KEY_SUFFIX_DENYLIST: tuple[str, ...] = ("_secret", "_password", "_token")


def secret_named_evidence_keys(evidence: Mapping[str, Any]) -> frozenset[str]:
    """Return the ``evidence`` keys :class:`FailureDetails` rejects as secret-named.

    The envelope's validator refuses secret-named keys outright, so a producer
    holding a rejected verdict has no way to ask *which* key was the problem
    without re-deriving the denylist. Exposing the predicate keeps that single
    source of truth here, at the wire layer where the rule lives.

    Args:
        evidence: Candidate evidence mapping, keyed by the producing
            dataclass's field names.

    Returns:
        The offending keys, empty when the mapping is acceptable.

    Example:
        >>> sorted(secret_named_evidence_keys({"host": "db", "api_key": "x"}))
        ['api_key']
    """
    return frozenset(
        k
        for k in evidence
        if k.lower() in _EVIDENCE_KEY_DENYLIST
        or any(k.lower().endswith(s) for s in _EVIDENCE_KEY_SUFFIX_DENYLIST)
    )


class FailureDetails(BaseModel):
    """Pydantic envelope serialized into ``ApplicationError.details=[…]``.

    Round-trips through ``pydantic_data_converter`` without any dict adapter.
    Consumers read routing fields (``category``, ``code``, ``retryable``,
    ``audience``) as typed attributes; per-error context lives in ``evidence``,
    whose keys match the dataclass fields of the Error that produced it.

    Field semantics:
    - ``category``: the closed FailureCategory enum — what happened.
    - ``audience``: who needs to act (USER / PLATFORM / APP_OWNER). Closed
      three-value enum; every leaf must pick one. There is no UNKNOWN
      escape hatch — if the locus is unclear the answer is APP_OWNER
      (the team that wrote the code investigates and reclassifies).
    - ``code``: app-owned string for fine-grained identification.
    - ``suggested_action``: optional imperative hint ("regrant Glue read access").
      The voice shifts with the audience: customer-facing text when
      ``audience=USER``, engineer-facing remediation when ``audience=APP_OWNER``,
      runbook hint when ``audience=PLATFORM``.
    - ``evidence``: per-error context whose schema is the producing dataclass.

    Tenant identity is intentionally NOT carried on this envelope. Per-tenant
    attribution is the consumer's job — the producer (the failing app) does
    not know or carry tenant context. The Automation Engine (or any other
    consumer that reads ``ApplicationError.details``) attaches tenant from
    its own context when it ingests the failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: FailureCategory
    code: str
    retryable: bool
    audience: Audience = Audience.APP_OWNER
    message: str
    suggested_action: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    app_name: str | None = None
    run_id: str | None = None
    cause_repr: str | None = None

    @field_validator("message", "suggested_action")
    @classmethod
    def _redact_free_text(cls, v: str | None) -> str | None:
        """Scrub credentials out of the handler-authored strings, once, here.

        These two fields are the only free text on the envelope and both are
        written by the app, so a driver's connection string or a presigned URL
        lands in them routinely. This model is what reaches Temporal history,
        the Automation Engine and every log row, so redacting where it is built
        covers every consumer at once — including the ones not written yet.
        Idempotent, so an envelope replayed off the wire is unchanged.
        ``evidence`` is handled below by key name instead: it is structured,
        and a secret-named key is a producer bug worth rejecting, not masking.
        """
        return v if v is None else redact_secrets(v)

    @field_validator("evidence")
    @classmethod
    def _no_secret_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = secret_named_evidence_keys(v)
        if bad:
            raise ValueError(  # stdlib-interop: pydantic field_validator requires ValueError
                "evidence keys may not use secret-named fields: %s" % sorted(bad)
            )
        return v


# ── Reverse lookup: FailureDetails → AppError ────────────────────────────────

_LeafT = TypeVar("_LeafT", bound=type[AppError])

#: Field names every ``AppError`` declares on the base dataclass. They travel as
#: the envelope's own fields, never as evidence, so reconstruction passes them
#: explicitly and keeps them out of the evidence it splats onto a leaf.
_BASE_ERROR_FIELD_NAMES: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(AppError)
)

#: Wire ``code`` → the leaf that rebuilds it. Explicit rather than derived from
#: ``AppError.__subclasses__()``: more than one class shares a code today (a
#: subclass that inherits its parent's, or two leaves in different packages
#: naming the same failure), so an implicit map would pick one at random.
_WIRE_LEAVES: dict[str, type[AppError]] = {}
#: Guards the check-then-write in :func:`register_wire_leaf`, so two threads
#: claiming one code cannot both pass the conflict check.
_WIRE_LEAVES_LOCK = threading.Lock()

#: Keywords :func:`error_from_failure_details` passes to every registered leaf.
_REBUILD_KEYWORDS: frozenset[str] = frozenset(
    {"message", "retryable", "suggested_action", "app_name", "run_id"}
)


def _missing_rebuild_keywords(leaf: type[AppError]) -> frozenset[str]:
    """The rebuild keywords ``leaf``'s constructor cannot take.

    A leaf with a hand-written ``__init__`` (``init=False`` dataclass) may not
    accept the base fields the rebuild passes, and would raise ``TypeError`` on
    the far side of the wire instead of at registration.
    """
    params = inspect.signature(leaf).parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return frozenset()
    accepted = {
        p.name
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return _REBUILD_KEYWORDS - accepted


def register_wire_leaf(leaf: _LeafT) -> _LeafT:
    """Register ``leaf`` as the class :func:`error_from_failure_details` rebuilds for its ``code``.

    Usable as a class decorator. A code with no registered leaf still rebuilds,
    on a generic leaf that keeps the envelope's routing, so registering is what
    buys the leaf's own type and its declared evidence fields back — not
    correct routing, which an unregistered code keeps anyway.

    Re-registering the same class is a no-op, so a module re-imported (the
    Temporal workflow sandbox does this) does not fail.

    Args:
        leaf: A concrete ``AppError`` subclass whose ``code`` ClassVar names it.

    Returns:
        ``leaf``, unchanged.

    Raises:
        TypeError: ``code`` is already registered to a different class (one
            code rebuilds as one class; two would make the rebuilt type depend
            on import order), or ``leaf``'s constructor does not take the
            base fields the rebuild passes (``message``, ``retryable``,
            ``suggested_action``, ``app_name``, ``run_id``).

    Example:
        >>> @register_wire_leaf
        ... @dataclass(kw_only=True)
        ... class WidgetMissingError(NotFoundError):
        ...     code: ClassVar[str] = "NOT_FOUND_WIDGET"
    """
    missing = _missing_rebuild_keywords(leaf)
    if missing:
        raise TypeError(  # stdlib-interop: programming error at import, before any AppError context exists
            "%s.%s cannot be a wire leaf: its constructor does not take %s"
            % (leaf.__module__, leaf.__qualname__, sorted(missing))
        )
    with _WIRE_LEAVES_LOCK:
        existing = _WIRE_LEAVES.get(leaf.code)
        # Same qualified name means the same class re-created by a sandbox
        # re-import, not a second claim on the code.
        if existing is not None and (existing.__module__, existing.__qualname__) != (
            leaf.__module__,
            leaf.__qualname__,
        ):
            raise TypeError(  # stdlib-interop: programming error at import, before any AppError context exists
                "wire code %r is already registered to %s.%s"
                % (leaf.code, existing.__module__, existing.__qualname__)
            )
        _WIRE_LEAVES[leaf.code] = leaf
    return leaf


# The base ``TIMEOUT`` code is registered alongside any probe-specific timeout
# so a verdict from any producer still rebuilds as a timeout rather than
# degrading to a generic leaf.
for _leaf in (AuthError, AppTimeoutError, SourceUnavailableError, InternalError):
    register_wire_leaf(_leaf)
del _leaf

#: Fields :class:`_EnvelopeError` adds that carry routing or wire passthrough,
#: not per-leaf evidence.
_ENVELOPE_ROUTING_FIELDS: frozenset[str] = frozenset(
    {
        "category_override",
        "audience_override",
        "code_override",
        "cause_repr",
        "evidence",
    }
)


@dataclasses.dataclass(kw_only=True)
class _EnvelopeError(AppError):
    """Reconstruction leaf for a wire ``code`` this consumer has no leaf for.

    Routing (``category`` / ``audience``) is taken from the envelope, not from
    class-level constants, so a newer producer's verdict keeps its original
    routing instead of degrading to ``INTERNAL`` / ``APP_OWNER``.

    ``category`` / ``audience`` are instance values here, but the base
    ``to_failure_details()`` reads ``audience`` class-level
    (``type(self).audience``) — which would return the raw ``property`` object
    on this subclass and fail validation. The serializer is therefore
    overridden to read both instance-level, so a reconstructed unknown-code
    error round-trips.

    ``code`` / ``cause_repr`` / ``evidence`` are likewise carried as
    passthrough instance fields so the producer's diagnostic context and
    fine-grained code survive re-serialisation — not just the routing.
    """

    category_override: FailureCategory
    audience_override: Audience
    code_override: str | None = None
    cause_repr: str | None = None
    evidence: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def category(self) -> FailureCategory:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self.category_override

    @property
    def audience(self) -> Audience:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self.audience_override

    @property
    def code(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        # ``is not None``, not truthiness: an envelope's ``code`` may be ``""``
        # and must re-serialise as itself, not as ``INTERNAL``.
        return self.code_override if self.code_override is not None else super().code

    def to_failure_details(self) -> FailureDetails:
        # Routing overrides and ``cause_repr`` are not evidence; the
        # producer's evidence returns as the envelope's evidence.
        evidence: dict[str, Any] = {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name not in _BASE_ERROR_FIELD_NAMES | _ENVELOPE_ROUTING_FIELDS
        }
        evidence.update(self.evidence)
        return FailureDetails(
            category=self.category,
            code=self.code,
            retryable=self.effective_retryable,
            audience=self.audience,
            message=self.message,
            suggested_action=self.suggested_action,
            evidence=evidence,
            app_name=self.app_name,
            run_id=self.run_id,
            cause_repr=self.cause_repr,
        )


#: What a value under a nested secret-named key is replaced with.
_MASKED = "***"


def _mask_secret_named(value: object) -> object:
    """Replace the value under every nested secret-named key with ``***``.

    Runs on :func:`redact_wire_value`'s output, which is acyclic and
    depth-bounded, so this walk needs no guards of its own.
    """
    if isinstance(value, dict):
        bad = secret_named_evidence_keys({k: None for k in value if isinstance(k, str)})
        return {
            k: _MASKED if k in bad else _mask_secret_named(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_secret_named(v) for v in value]
    if isinstance(value, tuple):
        fields = getattr(type(value), "_fields", None)
        if fields is None:
            return tuple(_mask_secret_named(v) for v in value)
        # A NamedTuple's field names are keys too: ``Creds(api_key=...)``
        # carries a secret-named key positionally. It takes its fields
        # positionally, not one iterable.
        bad = secret_named_evidence_keys({name: None for name in fields})
        namedtuple_cls = cast("Callable[..., object]", type(value))
        return namedtuple_cls(
            *(
                _MASKED if name in bad else _mask_secret_named(v)
                for name, v in zip(fields, value, strict=True)
            )
        )
    return value


def redact_evidence_value(value: object) -> object:
    """Redact one evidence value, including secrets under nested keys.

    The :class:`FailureDetails` denylist judges top-level evidence keys only,
    and :func:`redact_wire_value` redacts string *contents* only, so an opaque
    credential under a nested secret-named key (``{"conn": {"api_key": "…"}}``)
    passes both. This masks the value under any nested key the denylist would
    reject, then redacts every remaining string.

    Args:
        value: The value of one evidence key.

    Returns:
        The same shape with secrets masked.

    Example:
        >>> redact_evidence_value({"conn": {"api_key": "opaque", "host": "db"}})
        {'conn': {'api_key': '***', 'host': 'db'}}
    """
    return _mask_secret_named(redact_wire_value(value))


def _redact_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Redact every evidence value; top-level keys are the denylist's job."""
    return {k: redact_evidence_value(v) for k, v in evidence.items()}


def degraded_failure_details(err: AppError) -> FailureDetails:
    """Re-serialise a verdict the wire envelope rejected, keeping its routing.

    For when ``err.to_failure_details()`` raises — in practice a connector leaf
    carrying a secret-named evidence field (``api_key``, ``db_password``, …),
    which the :class:`FailureDetails` denylist refuses. Falling back to an
    untyped class-name / message classification instead would throw away the
    connector's explicit verdict and silently invert e.g. ``AuthError`` /
    ``USER`` into ``InternalError`` / ``APP_OWNER``.

    So degrade the *evidence*, never the routing: drop the offending keys, and
    if the envelope still refuses, drop evidence entirely. ``category`` /
    ``audience`` / ``code`` / ``retryable`` are read instance-level so a
    reconstruction leaf's property overrides are honoured alongside a normal
    leaf's class constants. Every remaining evidence value is redacted.

    Args:
        err: The classified verdict whose serialisation was rejected.

    Returns:
        The degraded envelope.

    Raises:
        ValidationError: Even the evidence-free envelope was refused. The
            caller has nothing typed left to send. Log it with
            :func:`redacted_validation_reason`, never ``str()`` or
            ``exc_info``: both embed the rejected input.
    """
    evidence: dict[str, Any] = {
        f.name: getattr(err, f.name)
        for f in dataclasses.fields(err)
        if f.name not in _BASE_ERROR_FIELD_NAMES
    }
    cause_repr = sanitize_cause_repr(err.cause) if err.cause else None
    if isinstance(err, _EnvelopeError):
        # Mirror its ``to_failure_details()``.
        for name in _ENVELOPE_ROUTING_FIELDS:
            evidence.pop(name, None)
        evidence.update(err.evidence)
        cause_repr = err.cause_repr or cause_repr

    dropped = secret_named_evidence_keys(evidence)
    kept = {k: v for k, v in evidence.items() if k not in dropped}

    def _envelope(attempt: dict[str, Any]) -> FailureDetails:
        return FailureDetails(
            category=err.category,
            code=err.code,
            retryable=err.effective_retryable,
            audience=err.audience,
            message=err.message,
            suggested_action=err.suggested_action,
            evidence=_redact_evidence(attempt),
            app_name=err.app_name,
            run_id=err.run_id,
            cause_repr=cause_repr,
        )

    try:
        return _envelope(kept)
    except ValidationError:
        # The evidence-free attempt only differs if the first carried something.
        if not kept:
            raise
    return _envelope({})


def redacted_validation_reason(exc: ValidationError) -> str:
    """Return a ``ValidationError``'s validator messages only, dropping the rejected input.

    ``str(ValidationError)`` embeds ``input_value=…`` — for a rejected
    ``evidence`` dict that is the secret-named payload the denylist exists to
    keep off the wire, so neither the exception nor ``exc_info`` may be logged.
    The ``msg`` fields name the offending keys and nothing else.

    Args:
        exc: A validation error raised building a :class:`FailureDetails`.

    Returns:
        The validator messages joined with ``"; "``.
    """
    return "; ".join(str(err.get("msg", "")) for err in exc.errors())


def error_from_failure_details(details: FailureDetails) -> AppError:
    """Rebuild a typed error from its wire envelope.

    The reverse of ``AppError.to_failure_details()``. ``code`` selects the leaf
    through the registry (:func:`register_wire_leaf`); ``evidence`` keys are the
    producing dataclass's field names, so they map straight back onto the
    leaf's constructor. Keys the leaf does not declare are dropped rather than
    raising — a newer producer must not break an older consumer replaying its
    history.

    An unregistered ``code`` (a newer producer than this consumer, or a leaf
    nobody registered) rebuilds on a generic reconstruction leaf that keeps the
    envelope's own ``category`` / ``audience`` / ``code`` / ``retryable`` /
    ``evidence`` / ``cause_repr``. Rebuilding it as ``InternalError`` instead
    would force ``INTERNAL`` / ``APP_OWNER`` and silently re-route a
    customer-owned failure to Atlan.

    Reconstruction re-redacts the free text it copies. Defence in depth, not
    the policy: :class:`FailureDetails` redacts ``message`` and
    ``suggested_action`` in a validator that also runs on ``model_validate``,
    so an envelope replayed off Temporal history arrives already scrubbed. It
    is kept because one idempotent pass is cheap and a hand-built envelope
    reaches here unvalidated. Nested evidence values are redacted too: the
    denylist judges the key name alone.

    Args:
        details: The envelope to rebuild.

    Returns:
        The registered leaf for ``details.code``, or the generic
        envelope-routed leaf when none is registered.

    Example:
        >>> err = error_from_failure_details(AuthError(message="denied").to_failure_details())
        >>> type(err).__name__
        'AuthError'
    """
    message = redact_secrets(details.message)
    suggested_action = (
        redact_secrets(details.suggested_action)
        if details.suggested_action is not None
        else None
    )
    leaf = _WIRE_LEAVES.get(details.code)
    if leaf is None:
        return _EnvelopeError(
            category_override=details.category,
            audience_override=details.audience,
            code_override=details.code,
            cause_repr=(
                redact_secrets(details.cause_repr)
                if details.cause_repr is not None
                else None
            ),
            evidence=_redact_evidence(details.evidence),
            message=message,
            retryable=details.retryable,
            suggested_action=suggested_action,
            app_name=details.app_name,
            run_id=details.run_id,
        )
    # ``declared`` excludes the base fields: they are passed explicitly below,
    # so an evidence key shadowing one (``message``, ``retryable``, …) would
    # crash reconstruction with ``TypeError: got multiple values``.
    # ``init=False`` fields are not constructor arguments either.
    declared = {
        f.name for f in dataclasses.fields(leaf) if f.init
    } - _BASE_ERROR_FIELD_NAMES
    evidence = _redact_evidence(
        {k: v for k, v in details.evidence.items() if k in declared}
    )
    return leaf(
        message=message,
        retryable=details.retryable,
        suggested_action=suggested_action,
        app_name=details.app_name,
        run_id=details.run_id,
        **evidence,
    )
