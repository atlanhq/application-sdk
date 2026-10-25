"""Single-key secret probing, shared by both credential resolvers.

In single-key mode every candidate field value is looked up as its own secret
store key, because nothing in the payload says which fields are ref-keys and
which are literals (a host, a port, an inline credential). A key the store
definitively doesn't have is therefore expected and skipped; the field keeps
its literal value.

A store *error* is judged by what it says about the lookup. An outage, a cold
start, or a transient answer (throttling, a transient server error) fails
resolution so the run retries: skipping those would send a real ref-key as the
credential value. Any other store answer (invalid name, access denied, a 4xx
rejection, unrecognised backend wording) logs a WARNING and keeps the value,
because nothing in the payload records which fields are secret names and a
literal such as a URL or database name is routinely rejected by some stores.
If it was a real credential key, the auth attempt fails with the ref-key as the
literal value, and the WARNING names it by hash.

Used by :mod:`application_sdk.credentials.agent` (agent-shape payloads) and
:mod:`application_sdk.infrastructure._dapr.credential_vault` (GUID configs).
Each caller keeps its own candidate selection and merge rules; the probing,
classification, redaction and summary logging live here once. Backend-opaque:
callers pass a ``fetch`` that already applies their cold-start retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import traceback
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar
from urllib.parse import quote

from application_sdk.errors import redact_secrets
from application_sdk.errors.leaves import (
    ColdStartRaceError,
    DaprSidecarUnreachableError,
)
from application_sdk.infrastructure.secrets import (
    SecretStoreError,
    SecretStoreTransientError,
    SecretStoreUnavailableError,
    SecretStoreUnreachableError,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

#: Bounded fan-out for the probe sweep (BLDX-1594). A probe holds its slot for
#: its whole retry ladder, so the sweep costs ``ceil(candidates / this)``
#: ladders — set above realistic payload field counts so the common case is one
#: wave. Bounded because the candidate set is open-ended (``extra.*``) and a
#: wide burst invites vault throttling.
MAX_CONCURRENT_SINGLE_KEY_PROBES = 8

SecretT = TypeVar("SecretT")

#: ``fetch(value, label)``: look up *value*, returning ``None`` (or an empty
#: value) when the store definitively has no such key and raising otherwise.
#: *label* is the hashed ``sha256:…`` form to use in any log or error text.
ProbeFetch = Callable[[str, str], Awaitable[SecretT | None]]


def hash_label(value: str) -> str:
    """``sha256:<8 hex>`` stand-in for a ref-key in logs and errors.

    Ref-key names encode secret-store topology (purpose, environment) and
    enable enumeration if logs leak, so they never appear raw.
    """
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:8]}"


async def probe_single_keys(
    candidates: Sequence[str],
    fetch: ProbeFetch[SecretT],
) -> list[tuple[str, SecretT]]:
    """Probe each candidate as a secret key; return the ones that resolved.

    Probes run **concurrently**, bounded by
    :data:`MAX_CONCURRENT_SINGLE_KEY_PROBES`. They were serial until BLDX-1594:
    a probe that misses pays a full Dapr retry ladder, so serial probing cost
    *ladder × number of literal-valued fields*.

    Returns ``(value, secret)`` pairs in **candidate order**, not completion
    order, so a caller that merges them gets the same last-writer-wins result
    serial probing gave.

    Resolving nothing is not an error: some deployments carry literal
    credentials rather than ref-keys, so no key is ever expected back.

    Raises:
        SecretStoreUnreachableError: A probe found the store unreachable for
            its whole cold-start budget.
        SecretStoreUnavailableError: A probe hit a transient outage after the
            store had already answered once.
        SecretStoreTransientError: The store answered a probe with a temporary
            failure (throttling, a transient server error). Retryable.
        SecretStoreError: A probe failed with a non-store exception (a bug).

        A non-transient store error (invalid name, access denied, a 4xx
        rejection) does not raise: the value is kept and a WARNING is logged
        (see :func:`_probe_one`).

        Every raised error is hash-labelled and carries no ``cause`` (see
        :func:`_probe_one`). When several probes fail, the first in candidate
        order is raised.
    """
    if not candidates:
        return []

    sem = asyncio.Semaphore(MAX_CONCURRENT_SINGLE_KEY_PROBES)

    async def _bounded(value: str) -> SecretT | None:
        async with sem:
            return await _probe_one(fetch, value)

    # return_exceptions=True: letting gather cancel siblings on the first raise
    # unwinds them mid-backoff and buries the real failure under CancelledError.
    results = await asyncio.gather(
        *(_bounded(value) for value in candidates), return_exceptions=True
    )

    resolved: list[tuple[str, SecretT]] = []
    for value, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            # Outage, cold start, transient answer or a bug: don't proceed on a
            # partial resolve.
            raise result
        if result is not None:
            resolved.append((value, result))

    # Counts only, since ref-keys encode secret-store topology.
    if resolved:
        logger.info(
            "single-key credential resolution: resolved %d of %d probed fields",
            len(resolved),
            len(candidates),
        )
    else:
        # INFO, not WARNING: for an inline-literal config this is the expected
        # steady state every run.
        logger.info(
            "single-key credential resolution resolved 0 of %d probed fields; "
            "all values used as-is (expected when the config carries literal "
            "credentials rather than ref-keys)",
            len(candidates),
        )
    return resolved


async def _probe_one(fetch: ProbeFetch[SecretT], value: str) -> SecretT | None:
    """Probe one candidate. ``None`` means "use the value as-is".

    That is either a key the store doesn't have, or a store answer that is not
    transient (invalid name, access denied, a 4xx rejection, unrecognised
    wording): the latter is logged at WARNING and the value kept, as SDK 3.49
    did. Outages, cold starts and transient answers raise instead.

    Every raised error is **hash-labelled and cause-free**, never the original:
    a ``SecretStoreError`` carries the raw ref-key in ``.secret_name`` and
    ``__str__()``, and its ``cause`` (the underlying httpx exception) can
    re-embed it via a percent-encoded request URL.
    """
    label = hash_label(value)
    try:
        secret = await fetch(value, label)
    except DaprSidecarUnreachableError as exc:
        # Terminal: the budget was exhausted without one usable answer, so this
        # is not a race that a later attempt warms — keep the terminal type so a
        # persistent outage stays distinguishable from a still-cold one. The
        # secret-free component/attempts/elapsed diagnostics are carried through.
        raise SecretStoreUnreachableError(
            label,
            component=exc.component,
            attempts=exc.attempts,
            elapsed_seconds=exc.elapsed_seconds,
        ) from None
    except ColdStartRaceError:
        # A *steady-state* blip, not an exhausted budget: budget exhaustion
        # always raises DaprSidecarUnreachableError (caught above), so a bare
        # ColdStartRaceError means the retry helper skipped its cold-start wait
        # — this component has already answered once — and gave up after the
        # short warm retry.
        raise SecretStoreUnavailableError(label) from None
    except SecretStoreTransientError:
        # The store answered with a temporary failure (throttling, a transient
        # server error): skipping would send a real ref-key as the credential,
        # so fail and let the run retry. Keep the subtype.
        raise SecretStoreTransientError(
            f"Secret store failed transiently while looking up single-key "
            f"ref-key {label}",
            secret_name=label,
            retryable=True,
        ) from None
    # conformance: ignore[E004] logger.warning with redacted traceback is emitted below; exc_info omitted intentionally to prevent secret ref-key leaking through stdlib traceback formatting
    except Exception as exc:
        # NOT exc_info=True: SecretStoreError.__str__ renders `secret=<ref-key>`
        # and its message embeds the backend cause, which can echo the raw
        # ref-key — that would undo the hashing in the same log record. Format
        # the traceback ourselves, redact known secret patterns, and scrub the
        # literal ref-key (which redact_secrets can't know), bounded to
        # standalone tokens so a short key like "DB" doesn't corrupt
        # "DB_CONNECTION". Also scrub the percent-encoded form: the httpx
        # exception's str() can embed the request URL, which encodes the
        # ref-key via quote(key, safe="") (see AsyncDaprClient.get_secret).
        safe_traceback = redact_secrets("".join(traceback.format_exception(exc)))
        for candidate in {value, quote(value, safe="")}:
            safe_traceback = re.sub(
                rf"(?<![\w-]){re.escape(candidate)}(?![\w-])",
                label,
                safe_traceback,
            )
        if isinstance(exc, SecretStoreError):
            # The store answered, but not "not found" and not transiently:
            # nothing says this field is a secret name (it may be a URL or a
            # database name the store rejects), so keep the value.
            logger.warning(  # conformance: ignore[E005,L004] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
                "single-key probe failed for ref-key %s (%s) — store error, "
                "treating as non-secret. If this was a real credential "
                "key, the auth attempt will fail with the ref-key as the "
                "literal value.\n%s",
                label,
                type(exc).__name__,
                safe_traceback,
            )
            return None
        # Not a store error: a bug, not "this field isn't a secret".
        logger.warning(  # conformance: ignore[E005,L004] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
            "single-key probe for ref-key %s failed with an unexpected error "
            "(%s); stopping credential resolution.\n%s",
            label,
            type(exc).__name__,
            safe_traceback,
        )
        raise SecretStoreError(
            f"Secret store failed while looking up single-key ref-key {label} "
            f"({type(exc).__name__})",
            secret_name=label,
        ) from None
    if not secret:
        # Key not in store — expected for non-secret fields (host, port,
        # region literals).
        logger.debug("single-key probe: %s not found in store", label)
        return None
    return secret
