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
import os
import re
import time
import traceback
from typing import TYPE_CHECKING, Any

import orjson

from application_sdk.common.transforms import transform_agent_credentials
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.errors import redact_secrets
from application_sdk.infrastructure.secrets import SecretNotFoundError
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.credentials.spec import AgentCredentialSpec
    from application_sdk.infrastructure.secrets import SecretStore

logger = get_logger(__name__)

#: The agent secret-bundle fetch is the FIRST Dapr call a workflow makes (during
#: preflight credential resolution). On SDR/agent runs it can race a Dapr sidecar
#: that is still finishing its cold start — daprd component init has been observed
#: up to ~75s on fresh CI runners, past the app's startup readiness gate —
#: surfacing as "SecretStoreError: Failed to get secret: All connection attempts
#: failed". Retry the fetch (any transient error EXCEPT a genuine missing secret)
#: until the store responds, bounded by this deadline so a truly-broken store
#: still fails rather than hanging. Env-overridable for pathological runners.
_BUNDLE_FETCH_MAX_WAIT_SECONDS: float = float(
    os.environ.get("ATLAN_AGENT_SECRET_FETCH_MAX_WAIT_SECONDS", "120")
)
_BUNDLE_FETCH_BASE_DELAY_SECONDS: float = 2.0
_BUNDLE_FETCH_MAX_DELAY_SECONDS: float = 10.0

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
          carries literal credentials inline.
    """
    raw = spec.to_raw_dict()

    if spec.key_type == "single-key":
        bundle = await _fetch_per_key_bundle(secret_store, raw)
        resolved_flat = _substitute(raw, bundle)
    elif spec.secret_path:
        bundle = await _fetch_bundle(secret_store, spec.secret_path)
        resolved_flat = _substitute(raw, bundle)
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


def _is_transient_store_error(exc: BaseException) -> bool:
    """Whether a bundle-fetch failure looks like the secret store / Dapr sidecar
    being *not yet reachable* (worth waiting on) vs. a *genuine* store error
    (bad auth, wrong path/binding — should fail fast).

    Type alone can't distinguish them — a cold sidecar and a misconfigured
    binding both surface as ``SecretStoreError`` — so this walks the
    cause/context chain and matches connection-class exceptions plus the
    canonical connection-failure text. It deliberately errs toward NOT retrying
    an unrecognised error, so a real misconfiguration still fails in
    milliseconds instead of blocking for the whole retry window.

    Transport-coupled: the class-name-token and text branches are tuned to the
    httpx-backed Dapr client (``_dapr/http.py``). If that transport is ever
    swapped (e.g. gRPC, whose connect failure is ``AioRpcError`` / "failed to
    connect to all addresses" — no connect/timeout/transport token), extend the
    token/text set below or this quietly stops retrying the cold-sidecar case.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        # ConnectionError / TimeoutError (+ subclasses like ConnectionRefused)
        # are unambiguously transport-level. NB: OSError is intentionally NOT
        # matched — PermissionError etc. are OSError subclasses and must fail
        # fast.
        if isinstance(cur, (ConnectionError, TimeoutError)):
            return True
        # Backend-agnostic: httpx.ConnectError/ConnectTimeout/ReadTimeout/
        # TransportError etc. (the Dapr HTTP client) by class-name token, so we
        # don't hard-depend on httpx here.
        type_name = type(cur).__name__.lower()
        if any(tok in type_name for tok in ("connect", "timeout", "transport")):
            return True
        text = str(cur).lower()
        if "all connection attempts failed" in text or "connection refused" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


async def _get_bundle_raw(secret_store: SecretStore, secret_path: str) -> Any:
    """``secret_store.get`` with a readiness retry for a cold Dapr sidecar.

    A genuine missing secret (``SecretNotFoundError``) is re-raised immediately.
    Other failures are retried **only if they look transient** (see
    :func:`_is_transient_store_error` — a not-yet-reachable sidecar/store), with
    capped exponential backoff until ``_BUNDLE_FETCH_MAX_WAIT_SECONDS`` elapses;
    a genuine store error (auth, bad binding/path) is re-raised at once. Pure
    retry wrapper — no behaviour change once the store responds.

    Deliberately layered on top of the transport's own retry: the Dapr HTTP
    client (``_dapr/http.py``) already wraps calls in a ``RetryTransport`` that
    retries the same connect/network errors on a short (~15s) budget — so each
    ``secret_store.get`` here spends that budget internally before this loop
    backs off. The long readiness wait lives here, at the agent layer, on
    purpose: it is scoped to the *idempotent* secret GET. Widening the transport
    budget instead would apply the same 120s wait to the non-idempotent POSTs
    (save_state / publish_event / invoke_binding / delete_state) that share the
    client — which we don't want. Keep both layers.
    """
    deadline = time.monotonic() + _BUNDLE_FETCH_MAX_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return await secret_store.get(secret_path)
        except SecretNotFoundError:
            raise
        # conformance: ignore[E004] transient-fetch retry; re-raises non-transient errors and the last error once the deadline passes
        except Exception as exc:
            remaining = deadline - time.monotonic()
            # Fail fast on a genuine (non-transient) store error, or once the
            # budget is spent.
            if remaining <= 0 or not _is_transient_store_error(exc):
                raise
            # Cap the backoff to the remaining budget so the total wait can't
            # overshoot _BUNDLE_FETCH_MAX_WAIT_SECONDS by a full delay.
            delay = min(
                _BUNDLE_FETCH_MAX_DELAY_SECONDS,
                _BUNDLE_FETCH_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
                remaining,
            )
            logger.warning(
                "Agent secret-bundle fetch at '%s' failed (attempt %d); the Dapr "
                "sidecar / secret store may still be starting — retrying in %.1fs: %s",
                secret_path,
                attempt,
                delay,
                exc,
            )
            await asyncio.sleep(delay)


async def _fetch_bundle(secret_store: SecretStore, secret_path: str) -> dict[str, Any]:
    """Fetch and JSON-parse the secret bundle at ``secret-path``."""
    try:
        raw = await _get_bundle_raw(secret_store, secret_path)
    except SecretNotFoundError as exc:
        raise CredentialNotFoundError(secret_path) from exc
    # conformance: ignore[E004] re-raises immediately as typed CredentialError with chained cause; logging deferred to caller boundary
    except Exception as exc:
        raise CredentialError(
            f"Failed to fetch agent secret bundle at '{secret_path}': {exc}",
            credential_name=secret_path,
            cause=exc,
        ) from exc

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
    """
    bundle: dict[str, Any] = {}
    seen: set[str] = set()

    async def _try_fetch(value: str) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        try:
            secret = await secret_store.get_optional(value)
        # conformance: ignore[E004] logger.warning with redacted traceback is emitted below; exc_info omitted intentionally to prevent secret ref-key leaking through stdlib traceback formatting
        except Exception as exc:
            # Store-side error — distinct from "key not in store" (silent
            # below). A transient outage here on a real secret field
            # would otherwise auth-fail with the ref-key as the literal
            # username, so surface at WARNING with the stack trace.
            # Log a hash, not the ref-key itself: ref-key names encode secret
            # store topology (purpose, environment) and enable enumeration if
            # logs leak.
            value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
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
            safe_traceback = re.sub(
                rf"(?<![\w-]){re.escape(value)}(?![\w-])",
                f"sha256:{value_hash}",
                redact_secrets("".join(traceback.format_exception(exc))),
            )
            logger.warning(  # conformance: ignore[E005,L004] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
                "single-key probe failed for ref-key sha256:%s — store error, "
                "treating as non-secret. If this was a real credential "
                "key, the auth attempt will fail with the ref-key as the "
                "literal value.\n%s",
                value_hash,
                safe_traceback,
            )
            return
        if secret in (None, ""):
            # Key not in store — expected for non-secret fields probed
            # in single-key mode (host, port, region literals).
            logger.debug(
                "single-key probe: sha256:%s not found in store (non-secret field)",
                hashlib.sha256(value.encode()).hexdigest()[:8],
            )
            return
        bundle[value] = secret

    for key, value in raw.items():
        if key in _LITERAL_KEYS:
            continue
        if isinstance(value, str):
            await _try_fetch(value)

    extra = raw.get("extra")
    if isinstance(extra, dict):
        for value in extra.values():
            if isinstance(value, str):
                await _try_fetch(value)

    return bundle


def _substitute(agent: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    """Replace ref-key string values in ``agent`` with values from ``bundle``.

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
    out: dict[str, Any] = dict(agent)
    for key, value in list(out.items()):
        if key in _LITERAL_KEYS:
            continue
        if isinstance(value, str) and value in bundle:
            out[key] = bundle[value]

    # v2-compat: descend into a nested ``extra`` dict if present.
    extra = out.get("extra")
    if isinstance(extra, dict):
        new_extra = dict(extra)
        for key, value in list(new_extra.items()):
            if isinstance(value, str) and value in bundle:
                new_extra[key] = bundle[value]
        out["extra"] = new_extra

    return out
