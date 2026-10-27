"""Agent-shape credential resolution for v3 inline agent payloads.

This module mirrors the behaviour of the v2
``application_sdk.services.secretstore.SecretStore.get_credentials()``
flow, but operates on an :class:`AgentCredentialSpec` (the typed
representation of the ``agent_json`` workflow field) rather than a
GUID-indexed blob in the state store.

The ``agent_json`` field on the workflow input describes *how to
resolve* a credential against an external secret manager (AWS Secrets
Manager, Azure Key Vault, K8s secrets) — **not** the secret values
themselves.  Its fields mix literal configuration values (``host``,
``port``, ``aws-region``) with **ref-keys**: string values that name
a field inside the secret bundle stored at ``secret-path``.

Resolution is a three-step process:

1. **Fetch the bundle** at ``secret-path`` via the injected
   :class:`~application_sdk.infrastructure.secrets.SecretStore`.  The
   concrete backend (Dapr + AWS Secrets Manager in production,
   :class:`~application_sdk.testing.mocks.MockSecretStore` under test)
   is opaque to this module.
2. **Substitute** each string value in the agent spec that matches a
   key in the bundle with the real value.  Mirrors v2's
   ``resolve_credentials`` — walks the root dict plus one level of
   ``extra`` when ``extra`` is a nested dict.  Literal keys
   (``host``, ``port``, ``aws-region`` and friends) are never treated
   as ref-keys.
3. **Expand dotted keys** into nested dicts — e.g.
   ``{"extra.database": "db"}`` becomes ``{"extra": {"database": "db"}}``.
   This undoes the flattening the Argo template does when serialising
   YAML ``--parameter`` values. The output is a flat ``dict[str, Any]``
   with any dotted roots (``extra``, ``basic``, …) nested into
   sub-dicts.

The returned shape is **client-agnostic**: it is the same flat-dict
convention v2's resolved credentials produced at the client boundary,
consumable by any SQL, REST, NoSQL, or cloud-storage client whose
``load()`` entry point takes ``dict[str, Any]``.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.common.transforms import transform_agent_credentials
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.credentials.spec import VALUE_SOURCES_KEY
from application_sdk.errors import (
    AppError,
    ColdStartRaceError,
    DaprSidecarUnreachableError,
    redact_secrets,
)
from application_sdk.errors.leaves import DependencyUnavailableError
from application_sdk.infrastructure import (
    DAPR_SECRET_STORE_COMPONENT,
    retry_past_dapr_cold_start,
)
from application_sdk.infrastructure._single_key_probe import (
    hash_label,
    probe_single_keys,
    scrubbed_traceback,
)
from application_sdk.infrastructure.secrets import (
    AgentSecretReadError,
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreTransientError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.infrastructure.secrets import SecretStore

logger = get_logger(__name__)

#: Root-level keys whose values are always literals, never ref-keys
#: into the secret bundle. Mirrors the contract the Atlan platform
#: emits for the agent-shape JSON payload.
_LITERAL_KEYS: frozenset[str] = frozenset(
    {
        "connectBy",
        "agent-name",
        "secret-manager",
        "secret-path",
        "aws-region",
        "aws-auth-method",
        "azure-auth-method",
        "host",
        "port",
        "auth-type",
        "agent-type",
        "key-type",
    }
)

#: Connection settings under ``extra`` whose values are literals, never
#: ref-keys: single-key probing skips them wherever the ``extra`` sits
#: (``extra.database``, ``basic.extra.database``, a nested ``extra`` dict).
#: A store that rejects such a value as a secret name (a URL's ``/``)
#: answers with a failure, not "not found", and that failure stops the whole
#: resolution. Probing only: a multi-key bundle may still substitute them.
_CONNECTION_LITERAL_EXTRA_FIELDS: frozenset[str] = frozenset(
    {"compiled_url", "database"}
)


def _is_connection_literal(key: str) -> bool:
    *parents, field = key.split(".")
    return (
        bool(parents)
        and parents[-1] == "extra"
        and field in _CONNECTION_LITERAL_EXTRA_FIELDS
    )


async def resolve_agent_credential(
    spec: AgentCredentialSpec,
    secret_store: SecretStore,
) -> dict[str, Any]:
    """Resolve a typed agent credential spec to a flat dict.

    Args:
        spec: The :class:`AgentCredentialSpec` parsed from the workflow
            input's ``agent_json`` field.
        secret_store: The :class:`SecretStore` injected into the app's
            :class:`~application_sdk.app.context.AppContext` at worker
            startup.

    Returns:
        A flat ``dict[str, Any]`` with all ref-keys substituted for
        their real values and dotted root keys (``extra.database``,
        ``basic.username``) collapsed into nested dicts.

    Raises:
        CredentialParseError: If the fetched bundle isn't valid JSON.
        CredentialNotFoundError: If the secret store does not have
            a bundle at ``secret_path``.
        SecretStoreError: For any other secret-store failure, in either
            mode — a dependency failure, not a credential one. The
            :class:`SecretStoreUnavailableError` /
            :class:`SecretStoreUnreachableError` subtypes mark an outage.

    Note:
        Resolution mode is selected by the spec:

        * ``key-type: single-key`` — each ref-key is fetched as a
          separate secret store entry (one entry per credential field).
          Useful for ``secretstores.local.env``-backed deployments
          where each env var holds one credential value, avoiding the
          all-in-one-JSON-bundle workaround. Non-secret fields
          (host/port) silently fall through unchanged.
        * ``secret-path`` set (and ``key-type`` not single-key): the
          bundle is fetched once from ``secret_path`` and ref-keys are
          substituted from it (the original v2 multi-key behavior).
        * Both empty: raw spec values are used as-is (no store
          lookup). Intended for dev/testing where ``agent_json``
          carries literal credentials inline. Only reachable via a
          direct call to this function — the routed path never gets
          here, because :meth:`AgentCredentialSpec.is_populated`
          returns False without a fetch anchor (``secret-path`` or
          ``key-type: single-key``) and routing falls through to
          ``credential_guid`` instead.
    """
    raw = spec.to_raw_dict()

    if spec.value_sources is not None:
        resolved_flat, _ = await _resolve_declared(
            secret_store, raw, spec.value_sources
        )
    elif spec.key_type == "single-key":
        bundle = await _fetch_per_key_bundle(secret_store, raw)
        resolved_flat, _ = _substitute(raw, bundle)
    elif spec.secret_path:
        bundle = await _fetch_bundle(secret_store, spec.secret_path)
        resolved_flat, _ = _substitute(raw, bundle)
    else:
        resolved_flat = raw

    return transform_agent_credentials(resolved_flat)


async def _resolve_declared(
    secret_store: SecretStore | None,
    raw: dict[str, Any],
    sources: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], int]:
    """Substitute the declared ``secretKeyRef`` fields of a format-2 payload.

    Only those fields reach the store; every other field is a literal (or a
    file reference) and is never looked up or replaced. Returns the flat dict
    without the ``value-sources`` marker, and the number of fields resolved.
    A payload declaring no refs needs no store.

    Raises:
        DependencyUnavailableError: Refs are declared but no secret store is
            available to resolve them.
    """
    flat = {k: v for k, v in raw.items() if k != VALUE_SOURCES_KEY}
    if not sources:
        return flat, 0
    if secret_store is None:
        raise DependencyUnavailableError(
            message="No secret store available to resolve declared agent secrets",
            service="secret_store",
        )
    values = await _fetch_declared_refs(secret_store, sources)
    flat.update(values)
    return flat, len(values)


async def _fetch_declared_refs(
    secret_store: SecretStore, sources: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Fetch each declared secret once and read each field's ``key`` from it.

    A declared ref is known to be a ref, so every failure is final: there is
    no literal to fall back to (FND-4077). Not-found (the secret, or the key
    inside it) is the user's config; anything else is the store's, keeping its
    outage subtype and ``retryable`` hint. Secret names are hash-labelled in
    every message and log, as on the probing path.

    Raises:
        CredentialNotFoundError: A declared secret, or the key inside it, is
            absent. Names the field and the hashed secret name.
        SecretStoreError: The store failed to answer (the subtypes
            :class:`SecretStoreUnavailableError` /
            :class:`SecretStoreUnreachableError` for an outage).
    """
    by_name: dict[str, list[tuple[str, str]]] = {}
    for field, ref in sources.items():
        by_name.setdefault(ref["name"], []).append((field, ref["key"]))

    resolved: dict[str, Any] = {}
    for name, wanted in by_name.items():
        label = hash_label(name)
        fields = ", ".join(field for field, _ in wanted)
        try:
            secret = await retry_past_dapr_cold_start(
                lambda name=name: secret_store.get(name),
                description=f"declared secret lookup for {label}",
                component=DAPR_SECRET_STORE_COMPONENT,
            )
        except SecretNotFoundError:
            raise CredentialNotFoundError(
                f"secret {label} declared for field '{fields}'"
            ) from None
        except Exception as exc:  # conformance: ignore[E004] scrubbed traceback logged here; a hash-labelled, cause-free SecretStoreError is raised
            logger.warning(  # conformance: ignore[L009,L004] the raise below drops the raw exception (`from None`), so this scrubbed dump is the only record of the cause; exc_info would re-log it unscrubbed
                "declared secret lookup for %s (field '%s') failed — secret "
                "store error:\n%s",
                label,
                fields,
                scrubbed_traceback(exc, name, label),
            )
            raise _declared_store_error(label, fields, exc) from None
        for field, key in wanted:
            resolved[field] = _pick_key(secret, name, key, field)
    return resolved


def _pick_key(secret: Any, name: str, key: str, field: str) -> Any:
    """Read *key* from a fetched secret.

    A bundle (Kubernetes, Vault, an AWS JSON secret) is a JSON object, and
    ``key`` picks the field. A single-value secret (Azure Key Vault, GCP, a
    plain AWS string) has ``key`` equal to ``name``, as Dapr documents, and
    the whole value is the secret.
    """
    bundle: Any = secret
    if isinstance(secret, str):
        try:
            bundle = orjson.loads(secret)
        except (
            orjson.JSONDecodeError
        ):  # conformance: ignore[E002] not JSON: a single-value secret, handled below
            bundle = None
    if isinstance(bundle, dict) and key in bundle:
        return bundle[key]
    if key == name and isinstance(secret, str):
        return secret
    raise CredentialNotFoundError(
        f"key {hash_label(key)} in secret {hash_label(name)} declared for "
        f"field '{field}'"
    )


def _declared_store_error(label: str, fields: str, exc: Exception) -> SecretStoreError:
    """Clean, cause-free store error for a failed declared lookup."""
    if isinstance(exc, DaprSidecarUnreachableError):
        return SecretStoreUnreachableError(
            label,
            component=exc.component,
            attempts=exc.attempts,
            elapsed_seconds=exc.elapsed_seconds,
        )
    if isinstance(exc, ColdStartRaceError):
        return SecretStoreUnavailableError(label)
    if isinstance(exc, SecretStoreTransientError):
        # Throttled or a transient server error: the store said "try again",
        # so retry it; nobody has a grant to fix.
        return SecretStoreTransientError(
            f"Secret store was temporarily unable to return the secret declared "
            f"for field '{fields}'.",
            secret_name=label,
            retryable=True,
        )
    if isinstance(exc, SecretStoreError):
        # The store answered and refused a field known to be a ref: the
        # customer's store or grants must change, so it goes to the user.
        return AgentSecretReadError(
            label, fields=fields, retryable=exc.effective_retryable
        )
    # Not a store answer (a bug in the fetch path): the platform's to fix.
    return SecretStoreError(
        f"Secret store lookup for the secret declared for field '{fields}' "
        f"failed ({type(exc).__name__}).",
        secret_name=label,
        retryable=exc.effective_retryable if isinstance(exc, AppError) else None,
    )


# Keep backward-compatible alias for existing callers and tests
async def resolve_agent_json(
    agent_json: str,
    secret_store: SecretStore,
) -> dict[str, Any]:
    """Resolve an agent-shape JSON string to a flat dict.

    Backward-compatible wrapper around :func:`resolve_agent_credential`
    that accepts a raw JSON string.

    Args:
        agent_json: JSON string carried on ``workflow_args["agent_json"]``.
        secret_store: The secret store instance.

    Returns:
        A flat ``dict[str, Any]`` with substituted and expanded credentials.
    """
    from application_sdk.credentials.spec import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
        AgentCredentialSpec,
    )

    spec = AgentCredentialSpec.model_validate(agent_json)
    return await resolve_agent_credential(spec, secret_store)


async def _fetch_bundle(secret_store: SecretStore, secret_path: str) -> dict[str, Any]:
    """Fetch and JSON-parse the secret bundle at ``secret-path``.

    The agent secret-bundle fetch is typically the first Dapr call a workflow
    makes, and on SDR runs it can race a sidecar still finishing its cold
    start. Retry mechanics (transient classification, capped backoff, the
    one-shot cold-start gate) live in
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start` —
    shared with the other credential-resolution paths that race the same
    sidecar. See that function's docstring for the full contract.
    """
    try:
        raw = await retry_past_dapr_cold_start(
            lambda: secret_store.get(secret_path),
            description=f"Agent secret-bundle fetch at '{secret_path}'",
            component=DAPR_SECRET_STORE_COMPONENT,
        )
    except SecretNotFoundError as exc:
        raise CredentialNotFoundError(secret_path) from exc
    except Exception as exc:  # conformance: ignore[E004] scrubbed traceback logged here at the boundary; a clean-message SecretStoreError is raised for the caller/UI
        # The secret store failed or is unreachable (distinct from a missing
        # bundle, handled above). Log the scrubbed underlying cause for
        # diagnosis, then raise a clean-message error WITHOUT chaining the raw
        # exception: Temporal surfaces the *innermost* ApplicationError to the
        # UI, so a chained httpx error would leak a raw
        # "HTTPStatusError: 500 ... http://localhost:3500/v1.0/secrets/..." string
        # (exactly the unfriendly message seen in preflight). ``from None`` keeps
        # the customer-facing message clean while the log preserves the detail.
        logger.warning(  # conformance: ignore[L009,L004] the raise below drops the raw exception (`from None`), so this scrubbed redact_secrets() dump is the only record of the underlying cause — context the caller does not have; exc_info=True would re-log the unscrubbed traceback
            "Agent secret-bundle fetch failed — secret store error:\n%s",
            redact_secrets("".join(traceback.format_exception(exc))),
        )
        raise _bundle_store_error(secret_path, exc) from None

    if isinstance(raw, dict):
        # Some SecretStore backends may return a dict directly; accept it.
        return raw
    try:
        bundle = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        raise CredentialParseError(
            f"Agent secret bundle at '{secret_path}' is not valid JSON: {exc}",
            credential_name=secret_path,
            cause=exc,
        ) from exc
    if not isinstance(bundle, dict):
        raise CredentialParseError(
            f"Agent secret bundle at '{secret_path}' must be a JSON object, "
            f"got {type(bundle).__name__}",
            credential_name=secret_path,
        )
    return bundle


def _bundle_store_error(secret_path: str, exc: Exception) -> SecretStoreError:
    """Clean, cause-free secret-store error for a failed bundle fetch.

    A store failure is a dependency failure, not a credential one: raising
    ``CredentialError`` (an ``AuthError``) here counted every throttled or
    unreachable vault as an AUTH failure blamed on the user (FND-4077). Keeps
    the outage subtype (terminal vs transient) and the ``retryable`` hint, so a
    throttled vault is retried rather than failing the run.
    """
    if isinstance(exc, DaprSidecarUnreachableError):
        return SecretStoreUnreachableError(
            secret_path,
            component=exc.component,
            attempts=exc.attempts,
            elapsed_seconds=exc.elapsed_seconds,
        )
    if isinstance(exc, ColdStartRaceError):
        return SecretStoreUnavailableError(secret_path)
    return SecretStoreError(
        "Secret store failed to return the secret bundle at the configured "
        "secret-path. Check that your secret store is healthy and that this "
        "app is allowed to read the secret-path.",
        secret_name=secret_path,
        retryable=exc.effective_retryable if isinstance(exc, AppError) else None,
    )


async def _fetch_per_key_bundle(
    secret_store: SecretStore, raw: dict[str, Any]
) -> dict[str, Any]:
    """Build a synthetic bundle by per-key lookups against the secret store.

    For ``key-type: single-key`` agent specs, each non-literal string field
    value is treated as its own secret store key. The returned bundle maps
    each successfully fetched ref-key to its real value, so that the
    existing :func:`_substitute` step can finish substitution unchanged.

    Keys the store definitively doesn't have are skipped — single-key mode
    probes every non-literal field value, so a non-secret field (like ``host``
    carrying a hostname) won't fail the resolution. Unmatched values then take
    the v2-parity fallthrough in :func:`_substitute` (left as-is).

    A store *failure* is never skipped: a throttled or access-denied lookup
    would otherwise leave the ref-key in place and send it as the credential
    value. Probing, classification and redaction are shared with the
    GUID-path vault via
    :func:`~application_sdk.infrastructure._single_key_probe.probe_single_keys`;
    each probe retries a cold-start race via
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start`
    (shared with :func:`_fetch_bundle` — both race the same sidecar).

    Raises:
        SecretStoreError: If any probe's store lookup failed (the subtypes
            :class:`SecretStoreUnavailableError` /
            :class:`SecretStoreUnreachableError` for an outage). Hash-labelled.
    """

    async def _fetch(value: str, label: str) -> str | None:
        return await retry_past_dapr_cold_start(
            lambda: secret_store.get_optional(value),
            description=f"single-key probe for {label}",
            component=DAPR_SECRET_STORE_COMPONENT,
        )

    outcomes: dict[str, str] = {}
    try:
        resolved = await probe_single_keys(
            _probe_candidates(raw), _fetch, outcomes=outcomes
        )
    finally:
        # In a finally: a sweep that raises still reports what each field was.
        _log_probe_outcomes(raw, outcomes)
    return dict(resolved)


#: Outcomes counted in the legacy probe telemetry, in a fixed order.
_PROBE_OUTCOMES = ("resolved", "absent", "refused", "transient", "outage", "error")


def _log_probe_outcomes(raw: dict[str, Any], outcomes: dict[str, str]) -> None:
    """One WARNING per legacy single-key resolution: each probed field's outcome.

    This is evidence for moving a published workflow to declared sources
    (FND-4407), not a verdict on its own. A ``resolved`` field is a real ref
    (``secretKeyRef``). ``absent`` and ``refused`` are inconclusive: a missing
    or denied lookup can also be a ref whose secret was never created, or one
    the SDR identity may not read. Before calling such a field a plain value,
    corroborate it (e.g. the workflow succeeded with it as a plain value). The
    record also tracks how many runs still use the legacy format. It is a
    WARNING because the format is deprecated.

    Keyed by *field name* only. ``outcomes`` is keyed by raw value, which may be
    a credential or a ref name, so it is never logged. The extras are scalar
    and ``atlan.``-prefixed, so they survive the object-store export an SDR
    uploads to Atlan.
    """
    if not outcomes:
        return
    by_field: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, str) and value in outcomes:
            by_field[key] = outcomes[value]
    extra_dict = raw.get("extra")
    if isinstance(extra_dict, dict):
        for field, value in extra_dict.items():
            if isinstance(value, str) and value in outcomes:
                by_field[f"extra.{field}"] = outcomes[value]
    counts = {name: 0 for name in _PROBE_OUTCOMES}
    for outcome in by_field.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    logger.warning(  # conformance: ignore[L009] deprecation telemetry, no exception context
        "agent_json legacy single-key probe outcomes (deprecated format; "
        "per-field evidence for migrating to declared sources): %s",
        ",".join(f"{field}={outcome}" for field, outcome in sorted(by_field.items())),
        extra={
            "atlan.agent_json.format": "legacy-single-key",
            "atlan.agent_json.secret_manager": str(raw.get("secret-manager") or ""),
            "atlan.agent_json.probe.outcomes": ",".join(
                f"{field}={outcome}" for field, outcome in sorted(by_field.items())
            ),
            **{
                f"atlan.agent_json.probe.{name}": count
                for name, count in counts.items()
            },
        },
    )


def _probe_candidates(raw: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated field values to probe as candidate secret keys.

    Collected up-front because the probes run concurrently — a ``seen`` set
    mutated per-probe would race.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            candidates.append(value)

    for key, value in raw.items():
        if key in _LITERAL_KEYS or _is_connection_literal(key):
            continue
        _add(value)

    extra = raw.get("extra")
    if isinstance(extra, dict):
        for field, value in extra.items():
            if field not in _CONNECTION_LITERAL_EXTRA_FIELDS:
                _add(value)

    return candidates


def _substitute(
    agent: dict[str, Any], bundle: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Replace ref-key string values in ``agent`` with values from ``bundle``.

    Returns the substituted dict and the number of fields that were actually
    replaced from the bundle (0 means nothing resolved — every field kept its
    literal placeholder).

    Mirrors v2's
    :meth:`application_sdk.services.secretstore.SecretStore.resolve_credentials`:

    * Walks every root-level key. If the key is in ``_LITERAL_KEYS`` or
      the value is not a string, it is left alone. Otherwise, if the
      string value is a key in ``bundle``, it is replaced with the
      bundle value.
    * If the root dict contains an ``extra`` key whose value is a
      nested dict (the v2-era shape), the same substitution is applied
      one level deep inside it. Mostly a no-op for v3 payloads which
      use dotted-flat keys instead.

    Missing ref-keys are left as-is (same as v2). Downstream code is
    expected to error cleanly if a required field is still a placeholder.
    """
    substituted = 0

    def _sub(container: dict[str, Any], key: str, value: Any) -> bool:
        nonlocal substituted
        if not isinstance(value, str):
            return False
        if value in bundle:
            container[key] = bundle[value]
            substituted += 1
            return True
        # Agent mode kept the field's literal value because the secret store had
        # no matching key. Logged at INFO (no secret value, only the field name)
        # so a customer debugging "why did auth fail" can see which field never
        # resolved and is being sent as its literal placeholder.
        logger.info(
            "agent mode: field '%s' not found in secret store; using literal value",
            key,
        )
        return False

    out: dict[str, Any] = dict(agent)
    for key, value in list(out.items()):
        if key in _LITERAL_KEYS:
            continue
        _sub(out, key, value)

    # v2-compat: descend into a nested ``extra`` dict if present.
    extra = out.get("extra")
    if isinstance(extra, dict):
        new_extra = dict(extra)
        for key, value in list(new_extra.items()):
            _sub(new_extra, key, value)
        out["extra"] = new_extra

    return out, substituted


@dataclass
class SecretStoreCheckResult:
    """Outcome of the SDR secret-store preflight probe.

    ``passed`` is the UI verdict. The two failure axes are tracked separately
    because a single flag conflated them:

    * ``store_down`` — the secret store *itself* is the failure (unreachable /
      erroring). The caller renders this as ``DEPENDENCY_UNAVAILABLE``; every
      other failure is a ``PRECONDITION`` config gap. A multi-key spec with no
      ``secret-path`` is a config gap, not an outage, so it is **not**
      ``store_down`` even though the store is never contacted.
    * ``fatal`` — the preflight cannot proceed past this: credentials can't be
      resolved, so connectivity has nothing to try. The caller short-circuits.
      A reachable-but-nothing-resolved result is *not* fatal (fields fall back to
      literals and connectivity still runs).

    ``substituted`` is the resolved-key count (for the message). ``resolved`` is
    the fully-substituted credential dict — returned so the caller reuses it
    instead of re-fetching the bundle from the store a second time; ``None``
    whenever nothing was successfully fetched (every fatal case)."""

    passed: bool
    store_down: bool
    fatal: bool
    substituted: int
    message: str
    resolved: dict[str, Any] | None = None
    # One imperative customer-facing next step for the failing case; None on a
    # pass. Set per scenario so the caller's typed FailureDetails carries a real
    # remediation instead of cramming it into ``message``.
    suggested_action: str | None = None


async def check_secret_store_access(
    spec: AgentCredentialSpec,
    secret_store: SecretStore | None,
) -> SecretStoreCheckResult:
    """Probe the customer secret store for the SDR interactive preflight.

    Never raises — returns a structured result the preflight renders as a check
    row. Failure modes, and how they map onto ``store_down`` / ``fatal``:

    1. **Store down** (``store_down``, ``fatal``) — a configured store errors or
       is unreachable. The store itself is the blocker (``SourceUnavailable``,
       retryable). A *missing* store is not this case: it is a permanent config
       gap, handled as a ``PRECONDITION`` (see below), not a store outage.
    2. **Unresolvable config** (``fatal`` only, ``store_down`` stays False) — no
       store is configured at all, or a multi-key (non single-key) spec has no
       ``secret-path`` so the ref-keys have nowhere to resolve from. A *config*
       gap, so it is a ``PRECONDITION`` (retryable=False); the store is never
       contacted.
    3. **Secret-path not found** (``fatal`` only) — the store is reachable but the
       configured ``secret-path`` doesn't exist, so credentials can't resolve.
    4. **Nothing resolved** (neither flag) — the store is reachable but not a
       single ref-key was substituted, so every credential field falls back to
       its literal value. A likely misconfiguration (surfaced as a failed row),
       but NOT fatal: a customer who put raw secrets directly in the config can
       still connect, so the preflight keeps running the connectivity checks.

    On any non-fatal outcome ``resolved`` carries the substituted credential dict
    so the caller can build credentials without a second store fetch.
    """
    if spec.value_sources is not None and (
        secret_store is not None or not spec.value_sources
    ):
        # A format-2 payload declaring no secret refs needs no store at all,
        # so a missing store is not a gap for it.
        return await _check_declared_sources(spec, secret_store)

    if secret_store is None:
        # No secret store configured is a permanent deployment config gap, not a
        # transient outage — the store doesn't exist, it isn't "down". So this is
        # a PreconditionError (retryable=False), NOT a retryable SourceUnavailable:
        # store_down stays False, same as the config-gap branches below.
        return SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=True,
            substituted=0,
            message="Secret store is not configured for the SDR deployment.",
            suggested_action="Configure a secret store on the SDR deployment.",
        )

    raw = spec.to_raw_dict()
    if spec.key_type != "single-key" and not spec.secret_path:
        # Multi-key (non single-key) resolution fetches the bundle from a
        # secret-path. With neither single-key probing nor a secret-path, the
        # ref-keys can never be resolved, so the credentials can't be used — fail
        # the check and short-circuit (nothing for connectivity to try). The
        # store is never contacted, so this is a config gap, NOT store_down.
        return SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=True,
            substituted=0,
            message="No secret-path is configured for this credential.",
            suggested_action="Set secret-path on the connection's credentials.",
        )

    try:
        if spec.key_type == "single-key":
            bundle = await _fetch_per_key_bundle(secret_store, raw)
        else:
            bundle = await _fetch_bundle(secret_store, spec.secret_path)
    except CredentialNotFoundError:
        # Store reachable, but the configured path is absent: credentials can't
        # resolve, so short-circuit (fatal) with a clean row — it's a config
        # problem (PRECONDITION), not a store outage.
        return SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=True,
            substituted=0,
            message="Configured secret store is accessible, but secret-path is not found.",
            suggested_action="Check that secret-path matches where the secrets are stored.",
        )
    except (
        DependencyUnavailableError,
        SecretStoreUnavailableError,
        CredentialError,
    ):
        return SecretStoreCheckResult(
            passed=False,
            store_down=True,
            fatal=True,
            substituted=0,
            message="Configured secret store is not accessible.",
            suggested_action="Check that the secret store is running and reachable from the SDR worker.",
        )

    resolved_flat, substituted = _substitute(raw, bundle)
    # Same substitution the resolver runs (resolve_agent_credential): return the
    # transformed dict so the caller reuses it instead of re-fetching the bundle.
    resolved = transform_agent_credentials(resolved_flat)
    if substituted == 0:
        return SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=False,
            substituted=0,
            message=(
                "Configured secret store is accessible, but secret reference "
                "could not be resolved. The specified value will be used as is."
            ),
            suggested_action=(
                "Check that the configured secret keys and secret-path exist in "
                "the store."
            ),
            resolved=resolved,
        )
    return SecretStoreCheckResult(
        passed=True,
        store_down=False,
        fatal=False,
        substituted=substituted,
        message=(
            f"Configured secret store is accessible - {substituted} secret(s) "
            "retrieved."
        ),
        resolved=resolved,
    )


async def _check_declared_sources(
    spec: AgentCredentialSpec, secret_store: SecretStore | None
) -> SecretStoreCheckResult:
    """The secret-store preflight row for a format-2 payload.

    Only declared refs are looked up, so a failure is never ambiguous. A
    missing secret, or one the store refuses (access denied), is the user's
    config: fatal, not ``store_down``, rendered as a PRECONDITION row. An
    outage is the store's: fatal and ``store_down``. A payload declaring no
    refs passes without touching the store.
    """
    sources = spec.value_sources or {}
    try:
        resolved_flat, substituted = await _resolve_declared(
            secret_store, spec.to_raw_dict(), sources
        )
    except CredentialNotFoundError as exc:
        return SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=True,
            substituted=0,
            message=(
                "Configured secret store is accessible, but a declared secret "
                f"reference was not found: {exc.credential_name}."
            ),
            suggested_action=(
                "Check that each field's secretKeyRef names a secret, and a key "
                "inside it, that exist in the secret store."
            ),
        )
    except AgentSecretReadError as exc:
        return SecretStoreCheckResult(
            passed=False,
            store_down=False,
            fatal=True,
            substituted=0,
            message=(
                "Configured secret store is reachable, but it refused a declared "
                f"secret reference: {exc.message}"
            ),
            suggested_action=exc.suggested_action,
        )
    except (DependencyUnavailableError, CredentialError):
        return SecretStoreCheckResult(
            passed=False,
            store_down=True,
            fatal=True,
            substituted=0,
            message="Configured secret store is not accessible.",
            suggested_action=(
                "Check that the secret store is running and that the SDR's "
                "secret-store identity can read the declared secrets."
            ),
        )
    resolved = transform_agent_credentials(resolved_flat)
    if not sources:
        message = "No secret references declared - all values are used as entered."
    else:
        message = (
            f"Configured secret store is accessible - {substituted} secret(s) "
            "retrieved."
        )
    return SecretStoreCheckResult(
        passed=True,
        store_down=False,
        fatal=False,
        substituted=substituted,
        message=message,
        resolved=resolved,
    )
