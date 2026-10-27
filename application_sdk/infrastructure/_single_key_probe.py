"""Single-key secret probing, shared by both credential resolvers.

In single-key mode every candidate field value is looked up as its own secret
store key, because nothing in the payload says which fields are ref-keys and
which are literals (a host, a port, an inline credential). A key the store
definitively doesn't have is therefore expected and skipped; the field keeps
its literal value.

Anything else — the store failing to answer — must stop resolution. Before
FND-4077 a store error was also skipped, so a throttled or access-denied vault
left the ref-key string in place and the connector logged in with it as the
password: an auth failure blamed on the user, plus a burned lockout attempt at
the source.

**Deprecated, time-boxed exception (FND-4407):** a caller may pass
``keep_value_on_store_answer=True`` to keep the value, with a WARNING, when the
store *answers* with something other than "not found" (an invalid name, access
denied). Outages (unreachable, cold start, unavailable) and recognised
temporary failures (:class:`SecretStoreTransientError`: throttling, transient
5xx) still stop resolution and retry, so FND-4077's transient case never keeps
a value. Only legacy agent payloads opt in: the probe can't tell a plain
username, database name, URL or role ARN from a ref-key, so stores that reject
those failed runs whose config hadn't changed. The GUID vault path stays
strict. Delete the flag, and probing with it, once every agent payload declares
its sources (``"format": 2``, FND-4361); those refs are looked up strictly and
never probed.

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

#: A value with this prefix is a file in the deployment object store
#: (``credentials.utils.OBJECT_STORE_PREFIX``, fetched by
#: ``resolve_credential_file``), never a secret name. Probing it fails the run
#: on a store that rejects ``/`` in names, such as the Kubernetes store.
#: Repeated here because importing ``credentials`` from this module is circular.
_FILE_REF_PREFIX = "objectstore://"

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


def scrubbed_traceback(exc: BaseException, value: str, label: str) -> str:
    """*exc*'s traceback, safe to log next to the hashed *label*.

    Redacts known secret patterns, then replaces the raw *value* (which
    ``redact_secrets`` can't know) with *label*. Bounded to standalone tokens
    so a short key like "DB" doesn't corrupt "DB_CONNECTION". Also scrubs the
    percent-encoded form: the httpx exception's str() can embed the request
    URL, which encodes the key via quote(key, safe="") (see
    AsyncDaprClient.get_secret).
    """
    safe = redact_secrets("".join(traceback.format_exception(exc)))
    for candidate in {value, quote(value, safe="")}:
        safe = re.sub(rf"(?<![\w-]){re.escape(candidate)}(?![\w-])", label, safe)
    return safe


async def probe_single_keys(
    candidates: Sequence[str],
    fetch: ProbeFetch[SecretT],
    *,
    keep_value_on_store_answer: bool = False,
) -> list[tuple[str, SecretT]]:
    """Probe each candidate as a secret key; return the ones that resolved.

    ``objectstore://`` file references are not probed: they are left as-is
    for ``resolve_credential_file`` to fetch.

    ``keep_value_on_store_answer`` opts a caller into the deprecated bridge
    (FND-4407, see the module docstring): a store answer that isn't "not
    found" keeps the value instead of failing. Only legacy agent payloads opt
    in; the GUID vault path stays strict.

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
        SecretStoreError: The store answered a probe with a failure rather than
            "not found" (throttled, access denied, KMS). Keeps the original
            ``retryable`` hint. With ``keep_value_on_store_answer``, raised
            only for a non-store exception from ``fetch``.

        Every raised error is hash-labelled and carries no ``cause`` (see
        :func:`_probe_one`). When several probes fail, the first in candidate
        order is raised.
    """
    # Trimmed for the check only: resolve_credential_file strips the value
    # before its own prefix check, so a padded ref is a file reference too.
    candidates = [c for c in candidates if not c.strip().startswith(_FILE_REF_PREFIX)]
    if not candidates:
        return []

    sem = asyncio.Semaphore(MAX_CONCURRENT_SINGLE_KEY_PROBES)

    async def _bounded(value: str) -> SecretT | None:
        async with sem:
            return await _probe_one(
                fetch, value, keep_value_on_store_answer=keep_value_on_store_answer
            )

    # return_exceptions=True: letting gather cancel siblings on the first raise
    # unwinds them mid-backoff and buries the real failure under CancelledError.
    results = await asyncio.gather(
        *(_bounded(value) for value in candidates), return_exceptions=True
    )

    resolved: list[tuple[str, SecretT]] = []
    for value, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            # Don't proceed on a partial resolve: a field whose store lookup
            # failed would be sent as its literal ref-key.
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


async def _probe_one(
    fetch: ProbeFetch[SecretT],
    value: str,
    *,
    keep_value_on_store_answer: bool = False,
) -> SecretT | None:
    """Probe one candidate. ``None`` means the store has no such key.

    Every failure is re-raised **hash-labelled and cause-free**, never as the
    original: a ``SecretStoreError`` carries the raw ref-key in
    ``.secret_name`` and ``__str__()``, and its ``cause`` (the underlying httpx
    exception) can re-embed it via a percent-encoded request URL.
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
    # conformance: ignore[E004] logger.warning with redacted traceback is emitted below; exc_info omitted intentionally to prevent secret ref-key leaking through stdlib traceback formatting
    except Exception as exc:
        # NOT exc_info=True: SecretStoreError.__str__ renders `secret=<ref-key>`
        # and its message embeds the backend cause, which can echo the raw
        # ref-key — that would undo the hashing in the same log record.
        safe_traceback = scrubbed_traceback(exc, value, label)
        if (
            keep_value_on_store_answer
            and isinstance(exc, SecretStoreError)
            and not isinstance(exc, SecretStoreTransientError)
        ):
            # The store answered, but not with "not found": an invalid name,
            # access denied. Deprecated bridge (FND-4407, see the module
            # docstring): keep the value as entered, as SDK <= 3.49 did,
            # because a probe can't tell this plain value from a ref-key. A
            # recognised throttle is excluded: it retries below instead.
            logger.warning(  # conformance: ignore[E005,L004] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
                "DEPRECATED single-key fallback: the secret store rejected a "
                "lookup of %s (%s), so the value is used as entered. If it is "
                "a secret name rather than a plain value, the connection will "
                "fail with it. This fallback is temporary: the workflow's "
                "credentials will move to the declared-source format, where "
                "secrets are looked up strictly and plain values never reach "
                "the store (FND-4361).\n%s",
                label,
                type(exc).__name__,
                safe_traceback,
            )
            return None
        # The store answered with a failure, not "not found". Stop: skipping
        # here would send the ref-key itself as the credential value.
        logger.warning(  # conformance: ignore[E005,L004] exc_info would bypass the secret-redacted traceback built above; safe_traceback included inline
            "single-key probe for ref-key %s failed — secret store error, "
            "not a missing key; stopping credential resolution.\n%s",
            label,
            safe_traceback,
        )
        raise SecretStoreError(
            f"Secret store failed while looking up single-key ref-key {label} "
            f"({type(exc).__name__})",
            secret_name=label,
            retryable=(
                exc.effective_retryable if isinstance(exc, SecretStoreError) else None
            ),
        ) from None
    if not secret:
        # Key not in store — expected for non-secret fields (host, port,
        # region literals).
        logger.debug("single-key probe: %s not found in store", label)
        return None
    return secret
