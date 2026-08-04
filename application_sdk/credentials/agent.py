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
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import orjson

from application_sdk.common.transforms import transform_agent_credentials
from application_sdk.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialParseError,
)
from application_sdk.errors import ColdStartRaceError, redact_secrets
from application_sdk.infrastructure import (
    DAPR_SECRET_STORE_COMPONENT,
    retry_past_dapr_cold_start,
)
from application_sdk.infrastructure.secrets import (
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreUnavailableError,
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

#: Bounded fan-out for the single-key probe sweep.
#:
#: Probes are independent point lookups, so they run concurrently: instead of
#: paying one retry ladder per literal-valued field, overlapping probes pay
#: ``ceil(candidates / this)`` ladders. A probe holds its slot for the whole
#: ladder — most of which is backoff sleep — so this value is what decides how
#: many ladders deep the sweep can get, not just how many sockets are open.
#: Set above the field count of realistic agent payloads (observed 3–4
#: probed fields, see BLDX-1594) so the common case is a single wave.
#:
#: Bounded rather than unbounded because a pathological payload (``extra.*`` is
#: unbounded — ``AgentCredentialSpec`` is ``extra="allow"``) would otherwise
#: burst one request per field at once: a cloud vault throttles that, and Dapr
#: reports a throttled secrets GET as a plain 500/``ERR_SECRET_GET`` —
#: indistinguishable from "key absent" — so a throttle storm degrades into
#: *silently* unresolved credentials. It also caps concurrent cold-start
#: waiters: in parallel every probe starts before any has armed
#: :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start`'s
#: per-component gate, so a genuinely cold sidecar would otherwise see one
#: waiter per candidate instead of a bounded few.
_MAX_CONCURRENT_SINGLE_KEY_PROBES = 8

#: :func:`_probe_one` outcomes. ``ABSENT`` means the store answered "no such
#: key" (the expected result for a literal-valued field); ``STORE_ERROR`` means
#: the store answered with a failure. They are distinguished because *all*
#: probes erroring is a store fault worth raising on, while all probes being
#: absent can legitimately mean "this spec has no secret refs".
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

    Probes run **concurrently**, bounded by
    :data:`_MAX_CONCURRENT_SINGLE_KEY_PROBES`. They were serial until
    BLDX-1594: a missing-key probe costs a full Dapr retry ladder (Dapr
    returns 500 for "no such key" and the transport retries it blind), so
    serial probing made resolution cost *ladder × number of literal-valued
    fields* — which exhausted the fixed ``prime_sql_auth`` activity budget
    before the source was ever contacted.

    Overlapping them makes the cost ``ceil(candidates / cap)`` ladders — one
    ladder for any payload within the cap, which covers realistic agent
    specs. Note it is *not* unconditionally one ladder: a probe holds its
    slot for its whole ladder, so a payload with more candidates than the cap
    still runs in successive waves. Order is irrelevant: each probe is an
    independent point lookup and results are merged by ref-key.

    A transient cold-start outage on a probe is retried via
    :func:`~application_sdk.infrastructure.retry_past_dapr_cold_start`
    (shared with :func:`_fetch_bundle` — both race the same sidecar). Only
    a genuine, non-transient store error falls through to the silent-skip
    path; an outage that exhausts the retry budget is raised instead,
    mirroring every other :func:`retry_past_dapr_cold_start` call site.

    Raises:
        SecretStoreUnavailableError: If any probe found the store
            unreachable (cold-start outage past the retry budget).
        SecretStoreError: If there was more than one candidate and *every*
            probe returned a store-level error with nothing resolved — the
            store is at fault, not the fields, and proceeding would
            authenticate with ref-keys as literal values.
    """
    candidates = _probe_candidates(raw)
    if not candidates:
        return {}

    # Probes are independent point lookups, so run them concurrently: N
    # overlapping retry ladders cost about one ladder instead of N. See
    # _MAX_CONCURRENT_SINGLE_KEY_PROBES for why this is bounded.
    sem = asyncio.Semaphore(_MAX_CONCURRENT_SINGLE_KEY_PROBES)

    async def _probe(value: str) -> tuple[str, str, Any]:
        async with sem:
            return await _probe_one(secret_store, value)

    # return_exceptions=True rather than letting gather cancel siblings on the
    # first raise: a sibling cancelled mid-backoff unwinds through
    # ``httpx_retries``' sleep and buries the real failure under
    # CancelledError tracebacks — the exact shape that made this class of
    # incident hard to read in the first place. Let every probe settle, then
    # decide below.
    results = await asyncio.gather(
        *(_probe(value) for value in candidates), return_exceptions=True
    )

    bundle: dict[str, Any] = {}
    store_errors = 0
    cold_start_exc: BaseException | None = None

    for result in results:
        if isinstance(result, BaseException):
            # Only _probe_one's ColdStartRaceError branch raises; anything
            # else is a genuine bug and must not be swallowed into
            # "this field isn't a secret".
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
        # The store never answered for at least one probe. Surface the outage
        # rather than proceeding with a partially-resolved credential.
        raise cold_start_exc

    if len(candidates) > 1 and store_errors == len(candidates):
        # Every probe hit a store-level error and nothing resolved. Proceeding
        # would authenticate with ref-keys as literal values and stack
        # ``failed_login_attempts`` on the source — the lockout the caller's
        # retry_max_attempts=1 exists to prevent — so fail loudly instead.
        #
        # Requires more than one candidate: with a single probe, "all probes
        # errored" is just "the one probe errored", which is genuinely
        # ambiguous (that field may simply not be a secret) and is the
        # documented swallow-and-fall-through case. The signal here is
        # aggregate — a store fault (denied credentials, throttling, wrong
        # component) hits every probe, whereas a non-secret field hits none.
        raise SecretStoreError(
            f"Single-key credential resolution failed: all {len(candidates)} "
            "secret-store probes returned a store error and no secret "
            "resolved. Treating this as a secret-store failure rather than "
            "proceeding with unresolved credentials.",
        )

    # The one positive-resolution record for this path. Counts only — ref-keys
    # encode secret-store topology (see _probe_one). Without this, a fully
    # unresolved credential is only inferable from the *absence* of DEBUG
    # lines, which is why this failure mode went unattributed.
    if bundle:
        logger.info(
            "single-key credential resolution: resolved %d of %d probed fields",
            len(bundle),
            len(candidates),
        )
    elif store_errors == 0:
        # Nothing resolved and no probe errored — the store answered "absent"
        # for every candidate.
        #
        # Not an exception: a genuinely secret-free spec (e.g. auth-type
        # noauth whose every field is a literal) is legitimate. But "absent"
        # is also what a *throttled* cloud vault looks like, because Dapr
        # reports a throttled secrets GET as a plain 500/ERR_SECRET_GET —
        # indistinguishable from a missing key (see
        # _dapr/client.py::classify_secret_fetch_error). So this is the only
        # record distinguishing "no secrets to resolve" from "could not
        # resolve any secrets", and it must be loud enough to attribute a
        # downstream auth failure to.
        #
        # Skipped when store_errors > 0: each of those probes already logged
        # its own WARNING carrying the same conclusion, so a summary here
        # would only duplicate it.
        logger.warning(
            "single-key credential resolution resolved 0 of %d probed fields — "
            "every probed value will be used as-is. If any of them was a real "
            "ref-key, the auth attempt will fail with the ref-key as the "
            "literal value.",
            len(candidates),
        )

    return bundle


def _probe_candidates(raw: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated field values to probe as candidate secret keys.

    Collected up-front rather than de-duplicated inside the probe itself: the
    probes run concurrently, so a ``seen`` set mutated per-probe would race
    and could issue the same lookup twice.
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
        SecretStoreUnavailableError: If the store never answered (cold-start
            outage that exhausted the retry budget).
    """
    value_hash = hashlib.sha256(value.encode()).hexdigest()[:8]
    try:
        secret = await retry_past_dapr_cold_start(
            lambda: secret_store.get_optional(value),
            description=f"single-key probe for sha256:{value_hash}",
            component=DAPR_SECRET_STORE_COMPONENT,
        )
    except ColdStartRaceError:
        # The store never actually answered — a cold-start outage that
        # exhausted the full retry budget, not "this field isn't a
        # secret" (that case is already collapsed to None by
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
