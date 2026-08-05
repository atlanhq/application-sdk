"""Credential resolution for a check run — one implementation for every path.

Lifted out of :mod:`application_sdk.execution._temporal.preflight_gate`, which was
the only path that resolved all four ways a credential can reach a check. The
others each handled a subset: the SDR activities dereferenced ``agent_json`` only,
and the HTTP routes could not dereference anything at all — they could check
credentials pasted into the form, but never the *stored* credential a real run
would use. That gap is the single largest source of "it passed in the UI and
failed on the run": the UI was not checking the same secret.

Resolution deliberately happens here rather than in a deterministic workflow
frame: a workflow forwards only secret-free references (see
:class:`~application_sdk.execution._temporal.preflight_gate.PreflightGateInput`),
and the dereference happens inside the activity.

The fail-open taxonomy is the subtle part and is preserved verbatim from the gate.
Resolution is *our* plumbing, not evidence about the customer's source, so the
default when it breaks is the opposite of the handler's: only a **provable**
credential absence is a fact the run can be blamed for. Everything else — a Dapr
blip, a slow vault, the resolver's collapsed "unexpected vault error" — must
propagate so the caller fails open. Getting this backwards is how a transport
error becomes "your credential is missing" and aborts a healthy run.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.credentials.ref import CredentialRef
from application_sdk.credentials.resolver import CredentialResolver
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors.leaves import DependencyUnavailableError
from application_sdk.handler.contracts import HandlerCredential
from application_sdk.infrastructure.context import get_infrastructure
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class CredentialSource(BaseModel):
    """Every way a credential can arrive at a check, in one secret-free-by-default shape.

    Satisfies :class:`~application_sdk.credentials.ref.CredentialResolvable`
    structurally (``extraction_method`` / ``credential_guid`` / ``agent_json``), so
    :meth:`CredentialRef.resolve_or_none` accepts it directly.
    """

    inline: list[HandlerCredential] = Field(default_factory=list)
    """Already-resolved credentials supplied by the caller.

    The interactive HTTP path: values the user typed into the config form, which
    may not be stored anywhere yet. **Takes precedence over the reference fields**
    — see :func:`resolve`.
    """

    extraction_method: str = ""
    """Routing mode (e.g. ``agent`` / ``direct``)."""

    credential_guid: str = ""
    """Platform credential GUID, for direct (vault) resolution."""

    agent_json: AgentCredentialSpec | None = None
    """Agent-shape spec, for inline (customer secret-manager) resolution."""

    credential_ref: CredentialRef | None = None
    """A pre-built reference, when the caller already has one."""

    named_ref_fields: dict[str, str] = Field(default_factory=dict)
    """Multi-credential apps: ``{ref_name: field_name_holding_the_guid}``.

    Copied from the app's ``ExtractionInput.preflight_credential_refs``. Additive
    to the single-credential triple, not a replacement — when set, resolution
    takes the named path and :attr:`inline` / the triple are not used.
    """

    field_values: dict[str, Any] = Field(default_factory=dict)
    """Where :attr:`named_ref_fields` looks its guid field names up.

    The raw extraction-input snapshot on the gate path; the request body
    elsewhere.
    """


def require_secret_store() -> Any:
    """Return the secret store, or raise so the caller fails open.

    A reference exists but there is nothing to dereference it with — an infra
    failure, not a valid empty-credential state. Raising routes to the caller's
    fail-open path instead of calling the handler with empty credentials, where a
    real infra outage would be reported to the customer as an auth failure.
    """
    infra = get_infrastructure()
    secret_store = infra.secret_store if infra is not None else None
    if secret_store is None:
        raise DependencyUnavailableError(
            message="No secret store available to resolve preflight credentials",
            service="secret_store",
        )
    return secret_store


def is_definitive_credential_absence(exc: BaseException) -> bool:
    """Whether ``exc`` *proves* the credential is genuinely not there.

    The resolver deliberately collapses any unexpected vault error into
    ``CredentialNotFoundError`` (see ``CredentialResolver.resolve_raw``) so that
    the handler, not the resolver, decides what a missing credential means. That
    is harmless when the caller fails open on everything, but under enforcement it
    would turn a transport blip into "your credential is missing" and abort a
    healthy run.

    So a not-found is only attributable to the source when it is *provably* an
    absence: no cause at all, or a cause that is itself a definitive
    ``SecretNotFoundError``. Anything else is a collapsed plumbing failure.
    """
    from application_sdk.infrastructure.secrets import (  # noqa: PLC0415 — avoid import cycle at module load
        SecretNotFoundError,
    )

    if not isinstance(exc, CredentialNotFoundError):
        return False
    cause = exc.__cause__ or exc.cause
    return cause is None or isinstance(cause, SecretNotFoundError)


async def resolve_named_refs(
    source: CredentialSource,
) -> dict[str, list[HandlerCredential]]:
    """Resolve a multi-credential app's named guids, grouped by ref name.

    One fail-open taxonomy, drawn from the resolver's own typed errors: a confirmed
    dependency outage (a ``CredentialVaultError`` wrapping a ``ColdStartRaceError``,
    or a ``DependencyUnavailableError``) propagates so the caller fails open — a
    Dapr blip must never read as a bad credential. Every other resolver failure
    becomes an empty group so the *handler* decides whether a missing credential is
    ``NOT_READY``: a genuine ``CredentialNotFoundError``, a plain
    ``CredentialVaultError``, or any unexpected vault error (the resolver collapses
    the latter two into ``CredentialNotFoundError``).
    """
    grouped: dict[str, list[HandlerCredential]] = {
        name: [] for name in source.named_ref_fields
    }
    present = {
        name: guid
        for name, field in source.named_ref_fields.items()
        if (guid := source.field_values.get(field))
    }
    # A declared ref whose guid field is absent resolves to an empty group —
    # fail-open-safe. The log level distinguishes the two causes (field names only,
    # never secrets): some refs resolving and others absent is most likely a typo in
    # a guid field name (warn); every ref absent is almost always a legitimate
    # no-credential trigger, e.g. an automation trigger with empty metadata (debug,
    # so it does not warn on every such run).
    missing = {
        name: field
        for name, field in source.named_ref_fields.items()
        if name not in present
    }
    if missing and present:
        logger.warning(
            "Some declared preflight credential ref(s) have no value in the request; "
            "verify the guid field names in preflight_credential_refs. Missing: %s",
            missing,
        )
    elif missing:
        logger.debug(
            "All declared preflight credential refs are absent from the request; "
            "resolving to empty groups. Missing: %s",
            missing,
        )
    if not present:
        return grouped

    resolver = CredentialResolver(require_secret_store())
    for name, guid in present.items():
        ref = CredentialRef(name=name, credential_type="unknown", credential_guid=guid)
        try:
            raw = await resolver.resolve_raw(ref) or {}
        except CredentialNotFoundError:
            raw = {}
        grouped[name] = HandlerCredential.list_from_raw(raw)
    return grouped


async def resolve(
    source: CredentialSource,
) -> tuple[list[HandlerCredential], dict[str, list[HandlerCredential]]]:
    """Resolve ``source`` to ``(credentials, credentials_by_name)``.

    Precedence, in order:

    1. **Named refs** (``named_ref_fields``) — multi-credential apps take this
       path; ``credentials`` stays empty and handlers read ``credentials_by_name``.
    2. **Inline credentials** — returned as given, dereferencing nothing.
    3. **The reference triple** — dereferenced against the secret store.

    Inline beats the triple deliberately. When a user edits a password in the
    config form and clicks *Test*, they are asking about the value they just
    typed, not the one currently stored; resolving the stored secret instead would
    make the button answer a question nobody asked. Conversely, a caller that
    holds only a reference — the pre-run gate, a re-test of an already-saved
    connection, a scheduled probe — now gets the *stored* credential checked on
    every path, which is what makes a UI result predictive of a real run.

    Any resolution failure propagates; deciding what to do with it is the caller's
    (see :func:`is_definitive_credential_absence` for the one case that is
    attributable to the source rather than to us).
    """
    if source.named_ref_fields:
        return [], await resolve_named_refs(source)
    if source.inline:
        return list(source.inline), {}
    # resolve_or_none already prefers a pre-built ``credential_ref`` over the
    # triple, so this covers all three reference shapes in one call — and keeps
    # this path byte-identical to the gate's original resolution.
    ref = CredentialRef.resolve_or_none(source)
    if ref is None:
        return [], {}
    raw = await CredentialResolver(require_secret_store()).resolve_raw(ref) or {}
    return HandlerCredential.list_from_raw(raw), {}
