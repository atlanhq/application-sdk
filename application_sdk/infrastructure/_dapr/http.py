"""Async Dapr HTTP client — replaces the sync ``dapr`` Python library.

Calls the Dapr sidecar HTTP API directly via ``httpx.AsyncClient``.
This avoids blocking the event loop (the ``dapr`` library uses sync gRPC)
while keeping the same sidecar + component model.

Endpoint reference: https://docs.dapr.io/reference/api/
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
import orjson
from httpx_retries import Retry, RetryTransport
from pydantic import BaseModel

from application_sdk.constants import (
    _HTTP_POOL_LIMITS,
    _HTTP_POOL_TIMEOUT_SECONDS,
    DEPLOYMENT_NAME,
    LOCAL_ENVIRONMENT,
)
from application_sdk.errors.leaves import ColdStartRaceError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_DAPR_HTTP_PORT = 3500
_DEFAULT_TIMEOUT = 30.0
# Retry budget spans the sidecar cold-start: with backoff_factor=1.0 the waits
# are ~1+2+4+8s, so a Dapr call issued just before daprd is accepting requests
# retries across ~15s rather than giving up in ~1.5s (the old total=3,
# backoff=0.5). Retries only fire on failures; healthy calls are unaffected.
_DEFAULT_RETRY_TOTAL = 5

# Dapr HTTP API v1.0 building blocks
_API_PREFIX = "/v1.0"

STATE_PATH = f"{_API_PREFIX}/state/{{store_name}}"
STATE_KEY_PATH = f"{_API_PREFIX}/state/{{store_name}}/{{key}}"
SECRET_STORE_PATH = _API_PREFIX + "/secrets/{store_name}/{key}"
SECRET_STORE_BULK_PATH = _API_PREFIX + "/secrets/{store_name}/bulk"
PUBLISH_PATH = f"{_API_PREFIX}/publish/{{pubsub_name}}/{{topic}}"
BINDING_PATH = f"{_API_PREFIX}/bindings/{{binding_name}}"
METADATA_PATH = f"{_API_PREFIX}/metadata"


_HEALTHZ_PATH = "/v1.0/healthz"
# How long to wait at startup for the Dapr sidecar's HTTP server to start
# accepting connections. The old 10s was too short for cold starts (image
# pull + daprd boot on a fresh CI runner routinely exceed it); when we
# proceeded before daprd was up, the first Dapr call — e.g. an agent
# secret-bundle fetch during a workflow's preflight — failed with
# "ConnectError: All connection attempts failed". We do NOT hold startup for
# full component init (a healthz 204): once daprd answers the probe at all the
# connection race is over, and per-component readiness is covered by each call
# site's retry_past_dapr_cold_start budget. Overridable via env for
# pathologically slow (or fast) environments.
_DEFAULT_SIDECAR_WAIT_TIMEOUT = float(
    os.environ.get("ATLAN_DAPR_SIDECAR_WAIT_TIMEOUT", "60")
)
_DEFAULT_SIDECAR_POLL_INTERVAL = 0.5


def _dapr_base_url() -> str:
    port = os.environ.get("DAPR_HTTP_PORT", str(_DEFAULT_DAPR_HTTP_PORT))
    return f"http://localhost:{port}"


#: Set once this worker process has confirmed the Dapr sidecar is reachable
#: via :func:`wait_for_dapr_sidecar` getting a 204 — Dapr only returns this
#: once *every* component has finished initializing, so this is a genuinely
#: holistic signal that short-circuits :func:`retry_past_dapr_cold_start` for
#: every component, not just the one it happened to call. A worker whose
#: startup probe timed out ("proceeding anyway") leaves this False, so its
#: first real Dapr call(s) still get a full retry budget instead of being
#: falsely told they're ready.
_dapr_sidecar_confirmed_ready: bool = False

#: Per-component readiness, set by :func:`retry_past_dapr_cold_start` once a
#: wrapped call gets a definitive answer (success or a non-transient
#: rejection) for that ``component``. Deliberately keyed by component —
#: unlike the holistic :data:`_dapr_sidecar_confirmed_ready` signal above, one
#: component answering definitively (e.g. the credential-vault's object-store
#: binding) says nothing about whether a *different* component (e.g. the
#: secret store) has finished initializing, so it must not short-circuit that
#: other component's own cold-start retry. Callers that hit the same
#: underlying Dapr component (e.g. every secret-store fetch, regardless of
#: which module resolves it) should pass the same ``component`` identifier so
#: they correctly share one gate instead of each paying their own wait.
_dapr_component_confirmed_ready: set[str] = set()

#: Shared ``component`` identifier for every call site that fetches secrets
#: via the Dapr secret-store component — the agent bundle fetch, single-key
#: probes, the named-credential resolver path, and the GUID/vault credential
#: path's secret fetch. They all race the same underlying component, so they
#: share one cold-start gate.
DAPR_SECRET_STORE_COMPONENT = "secretstore"

#: ``component`` identifier for the GUID/vault credential path's upstream
#: object-store binding fetch — a distinct Dapr component from
#: :data:`DAPR_SECRET_STORE_COMPONENT`, so it must not share that gate (see
#: :data:`_dapr_component_confirmed_ready`).
DAPR_UPSTREAM_BINDING_COMPONENT = "upstream-binding"


async def wait_for_dapr_sidecar(
    timeout: float = _DEFAULT_SIDECAR_WAIT_TIMEOUT,
    interval: float = _DEFAULT_SIDECAR_POLL_INTERVAL,
) -> None:
    """Poll /v1.0/healthz until the Dapr sidecar's HTTP API is reachable or
    timeout elapses.

    Returns as soon as daprd answers the probe at all:

    * 204 — every component has finished initializing. Arms
      :data:`_dapr_sidecar_confirmed_ready` so the first real Dapr call skips
      :func:`retry_past_dapr_cold_start`'s own wait; this probe already paid
      it.
    * any other status — daprd's HTTP API is up but a component is still
      initializing. Returns *without* arming the gate: the connection race
      this probe exists to close (daprd not yet accepting connections on a
      cold start) is over, and per-component readiness is already covered by
      each call site's :func:`retry_past_dapr_cold_start` budget. Holding
      startup for it would only delay the HTTP health bind.

    Only connection errors keep the loop polling to the deadline — those mean
    daprd's HTTP server has not come up yet. On timeout the function logs a
    warning and returns without arming the gate, so the first real call still
    gets a full retry budget.

    Skipped entirely in local dev — /v1.0/healthz returns 500 because
    AWS-backed components can't initialize without real credentials.
    All components have ignoreErrors: true so the sidecar works fine.
    """
    global _dapr_sidecar_confirmed_ready

    if DEPLOYMENT_NAME == LOCAL_ENVIRONMENT:
        logger.debug("Local dev — skipping Dapr sidecar health check")
        return

    url = f"{_dapr_base_url()}{_HEALTHZ_PATH}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            try:
                r = await client.get(url)
            # conformance: ignore[E004] sidecar health probe; connection errors are expected while daprd is still cold-booting and are logged at debug
            except Exception as exc:
                last_exc = exc
                logger.debug("Dapr sidecar poll failed", exc_info=True)
            else:
                if r.status_code == 204:
                    _dapr_sidecar_confirmed_ready = True
                    return
                # Reachable but not fully initialized: this is the load-bearing
                # "proceed early" decision (the only path that fires whenever
                # healthz never reaches 204, e.g. SDR / optional non-init
                # components). INFO, not DEBUG, so it survives the prod INFO
                # floor and leaves a breadcrumb; not WARNING, since this is an
                # expected steady state and per-call cold-start retries cover
                # component readiness.
                # conformance: ignore[L006] not a per-iteration log — this INFO fires at most once and immediately returns; it is the single "proceeded early" decision, not loop-body volume
                logger.info(
                    "Dapr sidecar HTTP API reachable (healthz=%d) but not fully "
                    "initialized; proceeding without the full-component wait — "
                    "per-call cold-start retries cover component readiness",
                    r.status_code,
                )
                return
            if loop.time() >= deadline:
                # Only the connection-error path can reach here (any HTTP
                # response returns above), so surface the last error — a
                # genuinely unreachable/misconfigured sidecar (wrong port, DNS,
                # perms) should be diagnosable, not just "timed out".
                logger.warning(
                    "Dapr sidecar not reachable after %.0fs — proceeding anyway",
                    timeout,
                    exc_info=last_exc,
                )
                return
            await asyncio.sleep(interval)


#: How long an idempotent Dapr-backed call may retry a transient
#: :class:`~application_sdk.errors.leaves.ColdStartRaceError` before giving
#: up. The agent secret-bundle fetch is typically the first Dapr call a
#: workflow makes and can race a cold sidecar (daprd component init has been
#: observed up to ~75s on fresh CI runners); every
#: :func:`retry_past_dapr_cold_start` caller shares this one budget, since
#: they all race the same sidecar. This is a floor, not a hard ceiling: each
#: retried ``call()`` can itself burn up to the transport's own internal
#: retry budget (~15s, see ``_DEFAULT_RETRY_TOTAL`` above) before raising, so
#: the worst-case total wait before final failure can exceed this value by
#: up to one attempt's duration. Env-overridable for pathological runners.
DAPR_COLD_START_MAX_WAIT_SECONDS: float = float(
    os.environ.get("ATLAN_DAPR_COLD_START_MAX_WAIT_SECONDS", "120.0")
)
DAPR_COLD_START_BASE_DELAY_SECONDS: float = 2.0
DAPR_COLD_START_MAX_DELAY_SECONDS: float = 10.0


async def retry_past_dapr_cold_start(
    call: Callable[[], Awaitable[_T]],
    *,
    description: str,
    component: str,
) -> _T:
    """Retry an idempotent Dapr-backed call past a cold sidecar.

    Retries a :class:`~application_sdk.errors.leaves.ColdStartRaceError` (a
    structurally transient failure — e.g. ``SecretStoreUnavailableError``)
    with capped exponential backoff until
    :data:`DAPR_COLD_START_MAX_WAIT_SECONDS` elapses. Branching on that
    marker rather than a single concrete exception type means any current or
    future ``DependencyUnavailableError`` subtype (secret store, state
    store, pub/sub, credential-vault config binding, ...) can opt into
    cold-start retry just by also inheriting ``ColdStartRaceError`` for its
    transient case — no new call site-specific check needed here. This is
    deliberately independent of ``retryable``/``effective_retryable``, which
    is a separate, general Temporal/wire-level retry hint — see
    ``ColdStartRaceError``'s docstring for why the two must not be
    conflated.

    Any other exception (a definitive rejection, e.g. ``SecretNotFoundError``
    or a 4xx ``SecretStoreError``, or an exception outside the
    ``ColdStartRaceError`` family entirely) is proof *this* component is
    reachable: it arms :data:`_dapr_component_confirmed_ready` for
    ``component`` and is re-raised immediately, never retried.

    Scoped to cold start, and scoped to ``component``: a call here skips the
    wait entirely once either :data:`_dapr_sidecar_confirmed_ready` is set
    (a holistic 204 from :func:`wait_for_dapr_sidecar`, which confirms every
    component at once) or ``component`` itself has already answered
    definitively. Callers that hit the same underlying Dapr component (see
    :data:`DAPR_SECRET_STORE_COMPONENT`, :data:`DAPR_UPSTREAM_BINDING_COMPONENT`)
    must pass the same identifier to share that gate; a different component
    answering says nothing about this one's readiness, so it does not
    short-circuit it. A later outage on an already-confirmed component is
    then a steady-state failure, left to surface via the backend's own
    (shorter) retry budget instead of being masked for the full deadline on
    every subsequent call.

    Args:
        call: Zero-arg async callable to retry, e.g. ``lambda:
            secret_store.get(name)``. Must be idempotent — it may be invoked
            more than once.
        description: Human-readable label for the retry-warning log line,
            e.g. ``"Agent secret-bundle fetch at 'foo'"``. Only ``description``
            and the failing exception's *type name* are logged on each
            retried attempt — never the exception's message/traceback, since
            a ``SecretStoreError``'s ``str()`` embeds ``secret=<name>`` and
            some callers pass a hashed/redacted ``description`` specifically
            to keep the raw secret/ref-key out of logs. Callers that need
            full diagnostics on terminal failure should log them at their
            own boundary, with whatever redaction they require.
        component: Identifies which Dapr component ``call`` talks to (e.g.
            :data:`DAPR_SECRET_STORE_COMPONENT`). Callers racing the same
            component must use the same identifier.
    """
    global _dapr_sidecar_confirmed_ready

    if _dapr_sidecar_confirmed_ready or component in _dapr_component_confirmed_ready:
        return await call()

    deadline = time.monotonic() + DAPR_COLD_START_MAX_WAIT_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            result = await call()
        except ColdStartRaceError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Gave up waiting, but the dependency never actually
                # answered — do NOT arm the gate, a later call should still
                # wait out the cold start rather than assume steady-state
                # readiness.
                raise
            # Cap the backoff to the remaining budget so the total wait can't
            # overshoot DAPR_COLD_START_MAX_WAIT_SECONDS by a full delay, and
            # cap the exponent so a very large max-wait override can't
            # overflow.
            delay = min(
                DAPR_COLD_START_MAX_DELAY_SECONDS,
                DAPR_COLD_START_BASE_DELAY_SECONDS * (2 ** min(attempt - 1, 10)),
                remaining,
            )
            # conformance: ignore[L004,E005] exc_info=True would attach str(exc)/traceback, which for a SecretStoreError embeds `secret=<name>` and the backend cause's message — undoing the hashed `description` some callers deliberately pass to keep the raw ref-key out of logs. Logging the exception TYPE only is intentional, not an oversight — see the description arg's docstring above.
            logger.warning(
                "%s failed (attempt %d, %s); the dependency is not yet "
                "reachable — retrying in %.1fs",
                description,
                attempt,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
        # conformance: ignore[E004] proof the component answered (definitively) — arms its cold-start gate and re-raises unchanged
        except Exception:
            _dapr_component_confirmed_ready.add(component)
            raise
        else:
            _dapr_component_confirmed_ready.add(component)
            return result


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class BindingResult(BaseModel):
    """Response from a Dapr binding invocation."""

    data: bytes | None = None
    metadata: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class AsyncDaprClient:
    """Thin async wrapper around the Dapr sidecar HTTP API.

    One instance should be created per application and shared across
    all infrastructure components (state store, secret store, etc.).
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        retries: int = _DEFAULT_RETRY_TOTAL,
    ):
        self._base_url = base_url or _dapr_base_url()
        transport = RetryTransport(
            transport=httpx.AsyncHTTPTransport(limits=_HTTP_POOL_LIMITS),
            retry=Retry(
                total=retries,
                backoff_factor=1.0,
                status_forcelist=[500, 502, 503, 504],
                # Pin the retried transport errors to httpx-retries' own default
                # set, stated explicitly so it can't silently narrow/widen if the
                # library changes its default. These three base classes cover
                # connect/read/WRITE/close/pool + protocol errors alike, so
                # POST/DELETE calls (save_state/publish_event/invoke_binding/
                # delete_state) keep the exact retry coverage they had before —
                # listing leaf classes like ConnectError/ReadError would have
                # narrowed it and dropped WriteError/WriteTimeout/CloseError.
                # NOTE: these errors were already retried by default; the real
                # change in this fix is the wider *budget* above (total 3->5,
                # backoff 0.5->1.0, ~1.5s -> ~15s) that bridges a daprd cold start.
                retry_on_exceptions=[
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.RemoteProtocolError,
                ],
            ),
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, pool=_HTTP_POOL_TIMEOUT_SECONDS),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncDaprClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # State Store
    # ------------------------------------------------------------------

    async def save_state(self, store_name: str, key: str, value: Any) -> None:
        resp = await self._client.post(
            STATE_PATH.format(store_name=store_name),
            json=[{"key": key, "value": value}],
        )
        resp.raise_for_status()

    async def get_state(self, store_name: str, key: str) -> bytes | None:
        resp = await self._client.get(
            STATE_KEY_PATH.format(store_name=store_name, key=key)
        )
        if resp.is_error:
            resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.content

    async def delete_state(self, store_name: str, key: str) -> None:
        resp = await self._client.delete(
            STATE_KEY_PATH.format(store_name=store_name, key=key)
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Secret Store
    # ------------------------------------------------------------------

    async def get_secret(self, store_name: str, key: str) -> dict[str, str]:
        resp = await self._client.get(
            SECRET_STORE_PATH.format(store_name=store_name, key=quote(key, safe=""))
        )
        resp.raise_for_status()
        return resp.json()

    async def get_bulk_secret(self, store_name: str) -> dict[str, dict[str, str]]:
        resp = await self._client.get(
            SECRET_STORE_BULK_PATH.format(store_name=store_name)
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Pub/Sub
    # ------------------------------------------------------------------

    async def publish_event(
        self,
        pubsub_name: str,
        topic: str,
        data: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> None:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if metadata:
            for k, v in metadata.items():
                headers[f"metadata.{k}"] = v
        resp = await self._client.post(
            PUBLISH_PATH.format(pubsub_name=pubsub_name, topic=topic),
            content=data,
            headers=headers,
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    async def invoke_binding(
        self,
        binding_name: str,
        operation: str,
        data: bytes | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BindingResult:
        """Invoke a Dapr output binding.

        Note:
            If ``data`` contains valid JSON it is embedded as a parsed
            object so that Dapr forwards a proper JSON body to HTTP
            bindings (avoids double-encoding).  Otherwise the raw bytes
            are decoded as UTF-8 text.  Binary data (protobuf, compressed)
            should be base64-encoded by the caller.
        """
        body: dict[str, Any] = {
            "operation": operation,
            "metadata": metadata or {},
        }
        if data:
            try:
                parsed = orjson.loads(data)
                if isinstance(parsed, (dict, list)):
                    body["data"] = parsed
                else:
                    body["data"] = data.decode("utf-8", errors="replace")
            # conformance: ignore[E009] JSON/Unicode decode fallback; raw string is the safe default
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
            ):
                body["data"] = data.decode("utf-8", errors="replace")
        resp = await self._client.post(
            BINDING_PATH.format(binding_name=binding_name),
            json=body,
        )
        resp.raise_for_status()
        return BindingResult(
            data=resp.content if resp.content else None,
            metadata={
                k: v
                for k, v in resp.headers.items()
                if k.lower().startswith("metadata.")
            },
        )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def get_metadata(self) -> dict[str, Any]:
        resp = await self._client.get(METADATA_PATH)
        resp.raise_for_status()
        return resp.json()


async def get_dapr_component_types() -> dict[str, str]:
    """Return a ``{component_name: component_type}`` map from the Dapr sidecar.

    Reads ``/v1.0/metadata`` and projects each registered component to its
    type, e.g. ``{"objectstore": "bindings.aws.s3", "secretstore":
    "secretstores.hashicorp.vault"}``. This lets a worker self-report which
    object/secret store binding it is actually wired to, independent of how it
    was deployed.

    Best-effort: returns ``{}`` if the sidecar is unreachable or the response
    is malformed. Never raises — callers treat a missing entry as unknown.
    """
    try:
        # Tight bound: this runs inline at worker startup and is pure
        # observability — never let a slow/flaky sidecar gate the worker.
        # Degrade to {} rather than stall on the default 30s x retries.
        async with AsyncDaprClient(timeout=2.0, retries=0) as client:
            meta = await client.get_metadata()
    except Exception:
        logger.warning("Could not read Dapr component metadata", exc_info=True)
        return {}

    types: dict[str, str] = {}
    # Dapr versions differ on the key: newer sidecars return "components",
    # older ones "registeredComponents" (mirrors is_dapr_component_registered).
    for component in meta.get("components") or meta.get("registeredComponents") or []:
        name = component.get("name")
        if name:
            types[name] = component.get("type", "")
    return types
