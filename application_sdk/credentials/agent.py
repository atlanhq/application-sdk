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

import asyncio
import hashlib
import re
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import orjson

from application_sdk.common.transforms import transform_agent_credentials
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.errors import (
    ColdStartRaceError,
    DaprSidecarUnreachableError,
    redact_secrets,
)
from application_sdk.errors.leaves import DependencyUnavailableError
from application_sdk.infrastructure import (
    DAPR_SECRET_STORE_COMPONENT,
    retry_past_dapr_cold_start,
)
from application_sdk.infrastructure.secrets import (
    SecretNotFoundError,
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

#: Bounded fan-out for the single-key probe sweep (BLDX-1594). A probe holds its
#: slot for its whole retry ladder, so the sweep costs
#: ``ceil(candidates / this)`` ladders — set above realistic payload field
#: counts so the common case is one wave. Bounded because ``extra.*`` is
#: unbounded (``AgentCredentialSpec`` is ``extra="allow"``) and a wide burst
#: invites vault throttling, which Dapr reports as the same ambiguous 500 it
#: uses for a missing key.
_MAX_CONCURRENT_SINGLE_KEY_PROBES = 8

#: :func:`_probe_one` outcomes. Both non-fatal; distinguished only so the
#: summary log doesn't duplicate a per-probe WARNING that already fired.
_PROBE_RESOLVED = "resolved"
_PROBE_ABSENT = "absent"
_PROBE_STORE_ERROR = "store_error"


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
        CredentialError: For any other secret-store failure.

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

    if spec.key_type == "single-key":
        bundle = await _fetch_per_key_bundle(secret_store, raw)
        resolved_flat, _ = _substitute(raw, bundle)
    elif spec.secret_path:
        bundle = await _fetch_bundle(secret_store, spec.secret_path)
        resolved_flat, _ = _substitute(raw, bundle)
    else:
        resolved_flat = raw

    return transform_agent_credentials(resolved_flat)


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
    except Exception as exc:  # conformance: ignore[E004] scrubbed traceback logged here at the boundary; a clean-message CredentialError is raised for the caller/UI
        # The secret store returned an error or is unreachable (distinct from a
        # missing bundle, handled above). Log the scrubbed underlying cause for
        # diagnosis, then raise a clean-message CredentialError WITHOUT chaining
        # the raw exception: Temporal surfaces the *innermost* ApplicationError to
        # the UI, so a chained httpx error would leak a raw
        # "HTTPStatusError: 500 ... http://localhost:3500/v1.0/secrets/..." string
        # (exactly the unfriendly message seen in preflight). ``from None`` keeps
        # the customer-facing message clean while the log preserves the detail.
        logger.warning(  # conformance: ignore[L009,L004] the raise below drops the raw exception (`from None`), so this scrubbed redact_secrets() dump is the only record of the underlying cause — context the caller does not have; exc_info=True would re-log the unscrubbed traceback
            "Agent secret-bundle fetch failed — secret store unreachable:\n%s",
            redact_secrets("".join(traceback.format_exception(exc))),
        )
        raise CredentialError(
            "Secret store is not reachable. Check that your secret store is "
            "running and reachable, and that the configured secret-path exists.",
            credential_name=secret_path,
        ) from None

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


async def _fetch_per_key_bundle(
    secret_store: SecretStore, raw: dict[str, Any]
) -> dict[str, Any]:
    """Build a synthetic bundle by per-key lookups against the secret store.

    For ``key-type: single-key`` agent specs, each non-literal string field
    value is treated as its own secret store key. The returned bundle maps
    each successfully fetched ref-key to its real value, so that the
    existing :func:`_substitute` step can finish substitution unchanged.

    Missing keys are silently skipped — single-key mode probes every
    non-literal field value, so a non-secret field (like ``host`` carrying
    a hostname) won't fail the resolution. Unmatched ref-keys then take
    the v2-parity fallthrough in :func:`_substitute` (left as-is, surfaced
    by downstream connect errors).

    Probes run **concurrently**, bounded by
    :data:`_MAX_CONCURRENT_SINGLE_KEY_PROBES`. They were serial until
    BLDX-1594: a missing-key probe pays a full Dapr retry ladder, so serial
    probing cost *ladder × number of literal-valued fields* and exhausted the
    fixed ``prime_sql_auth`` activity budget before the source was contacted.
    Overlapping them costs ``ceil(candidates / cap)`` ladders — not
    unconditionally one, since a probe holds its slot for its whole ladder.
    This bounds the symptom, not the cause; the durable fix is tracked in
    https://github.com/atlanhq/application-sdk/issues/2995.

    A transient cold-start outage on a probe is retried via
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start`
    (shared with :func:`_fetch_bundle` — both race the same sidecar). Only
    a genuine, non-transient store error falls through to the silent-skip
    path; an outage that exhausts the retry budget is raised instead,
    mirroring every other :func:`retry_past_dapr_cold_start` call site.

    Resolving *nothing* is not an error, at any count of store errors: some
    deployments carry literal credentials in the workflow config rather than
    ref-keys, so no key is ever expected back. Recorded at INFO, not raised.

    Raises:
        SecretStoreUnavailableError: If any probe found the store
            unreachable (cold-start outage past the retry budget).
    """
    candidates = _probe_candidates(raw)
    if not candidates:
        return {}

    sem = asyncio.Semaphore(_MAX_CONCURRENT_SINGLE_KEY_PROBES)

    async def _probe(value: str) -> tuple[str, str, Any]:
        async with sem:
            return await _probe_one(secret_store, value)

    # return_exceptions=True: letting gather cancel siblings on the first raise
    # unwinds them mid-backoff and buries the real failure under CancelledError.
    results = await asyncio.gather(
        *(_probe(value) for value in candidates), return_exceptions=True
    )

    bundle: dict[str, Any] = {}
    store_errors = 0
    cold_start_exc: BaseException | None = None

    for result in results:
        if isinstance(result, BaseException):
            # Only _probe_one's ColdStartRaceError branch raises; anything else
            # is a bug, not "this field isn't a secret".
            if isinstance(result, ColdStartRaceError):
                cold_start_exc = cold_start_exc or result
                continue
            raise result
        outcome, value, secret = result
        if outcome == _PROBE_RESOLVED:
            bundle[value] = secret
        elif outcome == _PROBE_STORE_ERROR:
            store_errors += 1

    if cold_start_exc is not None:
        # The store never answered at all — don't proceed on a partial resolve.
        raise cold_start_exc

    # Positive-resolution record: counts only, since ref-keys encode
    # secret-store topology. Its absence is why this went unattributed.
    if bundle:
        logger.info(
            "single-key credential resolution: resolved %d of %d probed fields",
            len(bundle),
            len(candidates),
        )
    elif store_errors == 0:
        # INFO, not WARNING: for an inline-literal config this is the expected
        # steady state every run. It is also indistinguishable from a throttled
        # vault (Dapr reports both as 500/ERR_SECRET_GET), so it states the fact
        # without asserting which. Skipped when store_errors > 0 — those probes
        # already logged their own WARNING.
        logger.info(
            "single-key credential resolution resolved 0 of %d probed fields; "
            "all values used as-is (expected when the config carries literal "
            "credentials rather than ref-keys)",
            len(candidates),
        )

    return bundle


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
        if key in _LITERAL_KEYS:
            continue
        _add(value)

    extra = raw.get("extra")
    if isinstance(extra, dict):
        for value in extra.values():
            _add(value)

    return candidates


async def _probe_one(secret_store: SecretStore, value: str) -> tuple[str, str, Any]:
    """Probe one candidate ref-key. Returns ``(outcome, value, secret)``.

    Raises:
        SecretStoreUnreachableError: If the store never answered across the
            whole cold-start budget (the terminal case).
        SecretStoreUnavailableError: If the store failed *without* the budget
            being spent — only reachable once this component has already
            answered once, so ``retry_past_dapr_cold_start`` short-circuits its
            wait and a later blip surfaces raw (the transient case).
    """
    value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
    try:
        secret = await retry_past_dapr_cold_start(
            lambda: secret_store.get_optional(value),
            description=f"single-key probe for sha256:{value_hash}",
            component=DAPR_SECRET_STORE_COMPONENT,
        )
    except DaprSidecarUnreachableError as exc:
        # Terminal: the budget was exhausted without one usable answer, so this
        # is not a race that a later attempt warms — keep the terminal type so a
        # persistent outage stays distinguishable from a still-cold one. Same
        # redaction as the transient branch below (hash label, no cause); the
        # secret-free component/attempts/elapsed diagnostics are carried through.
        raise SecretStoreUnreachableError(
            f"sha256:{value_hash}",
            component=exc.component,
            attempts=exc.attempts,
            elapsed_seconds=exc.elapsed_seconds,
        ) from None
    except ColdStartRaceError:
        # A *steady-state* blip, not an exhausted budget: budget exhaustion
        # always raises DaprSidecarUnreachableError and is caught above, so the
        # only way a bare ColdStartRaceError reaches here is
        # retry_past_dapr_cold_start short-circuiting its wait — this component
        # has already answered once, so the error propagates from `call()`
        # without any retry. Either way the store did not answer, which is not
        # "this field isn't a secret" (that case is already collapsed to None by
        # get_optional without raising). Propagate so the caller sees
        # a typed outage instead of silently proceeding with a
        # corrupt credential, mirroring the vault sibling
        # (credential_vault.py's _fetch_single_key_secrets).
        #
        # Not a bare `raise` and not `from exc`: SecretStoreUnavailableError
        # (the only ColdStartRaceError subtype this path raises) carries
        # the raw ref-key in both `.secret_name` and `__str__()`, and its
        # `cause` (the underlying httpx exception) can re-embed the same
        # ref-key via a percent-encoded request URL — the exact leak the
        # `except Exception` branch below scrubs at length. Re-raise a
        # hash-labelled, cause-free equivalent instead of the original.
        raise SecretStoreUnavailableError(f"sha256:{value_hash}") from None
    # conformance: ignore[E004] logger.warning with redacted traceback is emitted below; exc_info omitted intentionally to prevent secret ref-key leaking through stdlib traceback formatting
    except Exception as exc:
        # Genuine, non-transient store error — distinct from "key not
        # in store" (silent below). A real secret field hitting this would
        # otherwise auth-fail with the ref-key as the literal username,
        # so surface at WARNING with the stack trace.
        # Log a hash, not the ref-key itself: ref-key names encode secret
        # store topology (purpose, environment) and enable enumeration if
        # logs leak.
        # NOT exc_info=True: SecretStoreError.__str__ renders `secret=<ref-key>`
        # and its message embeds the backend cause, which can echo the raw
        # ref-key — that would undo the hashing above in the same log record.
        # Format the traceback ourselves, redact known secret patterns, and
        # additionally scrub the literal ref-key (which redact_secrets can't
        # know) so the topology stays hidden while diagnosis survives.
        # Bound the ref-key match to standalone tokens: a literal replace of
        # a short key like "DB" would corrupt "DB_CONNECTION"; the
        # lookarounds treat word chars and hyphens as identifier-continuation
        # so only whole-token occurrences are scrubbed.
        #
        # Also scrub the percent-encoded form: the chained httpx exception's
        # str() can embed the request URL, which encodes the ref-key via
        # quote(key, safe="") (see infrastructure/_dapr/http.py's
        # AsyncDaprClient.get_secret) — a ref-key with URL-unsafe characters
        # (space, "/", "=") would otherwise survive un-scrubbed in that form.
        safe_traceback = redact_secrets("".join(traceback.format_exception(exc)))
        for candidate in {value, quote(value, safe="")}:
            safe_traceback = re.sub(
                rf"(?<![\w-]){re.escape(candidate)}(?![\w-])",
                f"sha256:{value_hash}",
                safe_traceback,
            )
        logger.warning(  # conformance: ignore[E005,L004] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
            "single-key probe failed for ref-key sha256:%s — store error, "
            "treating as non-secret. If this was a real credential "
            "key, the auth attempt will fail with the ref-key as the "
            "literal value.\n%s",
            value_hash,
            safe_traceback,
        )
        return (_PROBE_STORE_ERROR, value, None)
    if secret in (None, ""):
        # Key not in store — expected for non-secret fields probed
        # in single-key mode (host, port, region literals).
        logger.debug(
            "single-key probe: sha256:%s not found in store (non-secret field)",
            value_hash,
        )
        return (_PROBE_ABSENT, value, None)
    return (_PROBE_RESOLVED, value, secret)


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
    spec: "AgentCredentialSpec",
    secret_store: "SecretStore | None",
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
