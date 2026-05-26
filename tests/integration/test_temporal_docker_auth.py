"""Integration tests: TemporalAuthManager refresh against real Docker Temporal with JWT auth.

These tests verify token-refresh behaviour using the local Docker Temporal
stack that enforces RS256 JWT authentication.  They complement
test_temporal_auth_token_propagation.py (which uses an in-process HS256
proxy) by exercising the full auth path against a real Temporal frontend.

Prerequisites
-------------
1. Generate keys (one-time; gitignored):
       bash local/temporal-auth/scripts/gen_keys.sh

2. Start the Docker stack:
       docker-compose -f local/temporal-auth/docker-compose.yml up

3. Register the default namespace (once per fresh stack):
       bash local/temporal-auth/scripts/setup_namespace.sh

Run
---
    uv run pytest tests/integration/test_temporal_docker_auth.py -m integration -s -v
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from temporalio import workflow
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.service import RPCError
from temporalio.worker import Worker

from application_sdk.execution._temporal.auth import (
    TemporalAuthConfig,
    TemporalAuthManager,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPORAL_HOST = "localhost:7233"
_NAMESPACE = "default"
_PRIVATE_KEY_PATH = (
    Path(__file__).resolve().parents[2] / "local" / "temporal-auth" / "private.pem"
)
_KID = "temporal-local-dev"

# Token lifetimes used by the mock service
_INITIAL_TOKEN_TTL_SECONDS = 5
_REFRESH_TOKEN_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _issue_rs256_jwt(
    private_key: bytes,
    ttl_seconds: int,
    permissions: list[str] | None = None,
) -> str:
    """Issue a signed RS256 JWT accepted by the Docker Temporal stack."""
    if permissions is None:
        # default:write covers ActionRead (DescribeNamespace called by the
        # SDK worker at startup) and ActionWorker (PollWorkflowTaskQueue).
        # default:worker alone is insufficient because it omits ActionRead.
        permissions = [f"{_NAMESPACE}:write"]
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "temporal-local",
            "sub": "test@temporal",
            "iat": now,
            "exp": now + ttl_seconds,
            "permissions": permissions,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": _KID},
    )


# ---------------------------------------------------------------------------
# Mock RS256 token service
# ---------------------------------------------------------------------------


class _Rs256MockTokenService:
    """Issues real RS256 JWTs signed with the Docker Temporal private key.

    First call → short-lived token (_INITIAL_TOKEN_TTL_SECONDS).
    Subsequent calls → long-lived token (_REFRESH_TOKEN_TTL_SECONDS).
    """

    def __init__(self, private_key: bytes) -> None:
        self._private_key = private_key
        self._call_count = 0
        self.current_expires_at: datetime | None = None

    async def get_token(self, *, force_refresh: bool = False) -> str:
        self._call_count += 1
        ttl = (
            _INITIAL_TOKEN_TTL_SECONDS
            if self._call_count == 1
            else _REFRESH_TOKEN_TTL_SECONDS
        )
        now = int(time.time())
        exp = now + ttl
        token = jwt.encode(
            {
                "iss": "temporal-local",
                "sub": "test@temporal",
                "iat": now,
                "exp": exp,
                # default:write covers both DescribeNamespace (read action,
                # called by the SDK worker at startup) and PollWorkflowTaskQueue
                # (worker action). default:worker alone omits ActionRead.
                "permissions": [f"{_NAMESPACE}:write"],
            },
            self._private_key,
            algorithm="RS256",
            headers={"kid": _KID},
        )
        self.current_expires_at = datetime.fromtimestamp(exp, tz=UTC)
        return token


# ---------------------------------------------------------------------------
# Noop workflow (sandboxed=False avoids __file__ issues in pytest)
# ---------------------------------------------------------------------------


@workflow.defn(name="docker-auth-noop", sandboxed=False)
class _DockerAuthNoopWorkflow:
    @workflow.run
    async def run(self) -> None:
        return


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def private_key() -> bytes:
    """Yield the RSA private key; skip test if Docker stack is unavailable."""
    if not _PRIVATE_KEY_PATH.exists():
        pytest.skip(
            f"RSA private key not found at {_PRIVATE_KEY_PATH}. "
            "Run: bash local/temporal-auth/scripts/gen_keys.sh"
        )

    pk = _PRIVATE_KEY_PATH.read_bytes()

    # Confirm Docker Temporal is up and auth is enforced.
    token = _issue_rs256_jwt(pk, 60, permissions=["temporal-system:admin"])
    try:
        client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=token)
        await client.service_client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=_NAMESPACE)
        )
    except Exception as exc:
        pytest.skip(
            f"Docker Temporal not available at {_TEMPORAL_HOST}: {exc}. "
            "Run: docker-compose -f local/temporal-auth/docker-compose.yml up && "
            "bash local/temporal-auth/scripts/setup_namespace.sh"
        )

    return pk


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_expired_token_is_rejected(private_key: bytes) -> None:
    """Expired RS256 tokens are rejected with PERMISSION_DENIED by Docker Temporal.

    This is the canary test: it confirms that the Docker stack actually enforces
    JWT expiry.  If this test ever starts PASSING unexpectedly, auth enforcement
    has gone fail-open and the regression tests below are no longer meaningful.

    Timeline
    --------
    t=0  connect with 3-second token (no background refresh started)
    t=5  token is expired; describe_namespace must raise RPCError
    """
    pk = private_key
    short_token = _issue_rs256_jwt(pk, 3)
    client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=short_token)

    # Confirm the token works while valid.
    await client.service_client.workflow_service.describe_namespace(
        DescribeNamespaceRequest(namespace=_NAMESPACE)
    )

    # Wait for the token to expire.
    await asyncio.sleep(5)

    # Any authenticated call must now be rejected.
    with pytest.raises(RPCError, match="[Rr]equest unauthorized"):
        await client.service_client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=_NAMESPACE)
        )


async def test_api_key_propagates_after_background_refresh(
    private_key: bytes,
) -> None:
    """client.api_key is updated by TemporalAuthManager background refresh.

    Timeline
    --------
    t=0  connect with 5-second token
    t=2  background refresh fires, issues 300-second token, sets client.api_key
    t=5  initial token expires
    t=7  describe_namespace must succeed (requires valid client.api_key)

    PASSES: api_key was updated; authenticated calls work after expiry.
    FAILS:  api_key still holds expired token; describe_namespace returns
            PERMISSION_DENIED.
    """
    pk = private_key
    token_svc = _Rs256MockTokenService(pk)
    manager = TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id="test",
            client_secret="test",
            token_url="http://localhost:0/unused",
        )
    )
    manager._token_service = token_svc

    initial_token = await manager.acquire_initial_token()
    client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=initial_token)

    # Fire the refresh at 2 s so the new token is in place before expiry at 5 s.
    manager._calculate_sleep_seconds = lambda: 2.0  # type: ignore[method-assign]
    manager.start_background_refresh(client)

    try:
        await asyncio.sleep(_INITIAL_TOKEN_TTL_SECONDS + 2.0)

        try:
            await client.service_client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=_NAMESPACE)
            )
        except RPCError as exc:
            pytest.fail(
                f"Authenticated call failed after initial token expired: {exc}. "
                "client.api_key was not updated by the background refresh."
            )

        assert token_svc._call_count >= 2, (
            f"Background refresh should have called get_token at least twice, "
            f"got {token_svc._call_count} call(s)."
        )
    finally:
        await manager.shutdown()


async def test_worker_survives_token_expiry_with_docker_temporal(
    private_key: bytes,
) -> None:
    """Worker stays alive and processes tasks past initial token expiry.

    Workflow tasks are submitted every second to force poll RPCs to return
    quickly (Temporal's long-poll can hold for up to 70 s on an empty queue).
    Tasks submitted after the initial 5-second token expires must still be
    processed, confirming that refreshed tokens propagate to poll RPCs.

    PASSES: worker processes tasks after refresh; token rotation is seamless.
    FAILS:  worker crashes when new poll RPCs carry the expired token and are
            rejected by Temporal's JWT auth.
    """
    pk = private_key
    task_queue = f"docker-auth-{int(time.time())}"

    token_svc = _Rs256MockTokenService(pk)
    manager = TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id="test",
            client_secret="test",
            token_url="http://localhost:0/unused",
        )
    )
    manager._token_service = token_svc

    initial_token = await manager.acquire_initial_token()

    # Worker client starts with a short-lived token; manager refreshes it.
    worker_client = await Client.connect(
        _TEMPORAL_HOST, tls=False, api_key=initial_token
    )

    # Admin client with a long-lived token for submitting test workflows.
    admin_token = _issue_rs256_jwt(pk, 3600, permissions=["temporal-system:admin"])
    admin_client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=admin_token)

    # Refresh fires at 2 s — before the 5-second token expires.
    manager._calculate_sleep_seconds = lambda: 2.0  # type: ignore[method-assign]

    worker = Worker(
        worker_client,
        task_queue=task_queue,
        workflows=[_DockerAuthNoopWorkflow],
    )
    worker_task = asyncio.create_task(worker.run())

    manager.start_background_refresh(worker_client)

    completed_post_expiry: list[bool] = []

    async def _submit_workflows() -> None:
        start = time.monotonic()
        for i in range(15):
            if worker_task.done():
                break
            try:
                handle = await admin_client.start_workflow(
                    _DockerAuthNoopWorkflow.run,
                    id=f"docker-auth-{int(time.time() * 1000)}-{i}",
                    task_queue=task_queue,
                )
                elapsed = time.monotonic() - start
                if elapsed > _INITIAL_TOKEN_TTL_SECONDS:
                    try:
                        await asyncio.wait_for(handle.result(), timeout=10.0)
                        completed_post_expiry.append(True)
                    except Exception:
                        completed_post_expiry.append(False)
            except Exception:  # noqa: BLE001,S110
                pass
            await asyncio.sleep(1.0)

    submit_task = asyncio.create_task(_submit_workflows())

    try:
        await asyncio.sleep(_INITIAL_TOKEN_TTL_SECONDS + 10.0)

        if worker_task.done():
            worker_exc: BaseException | None = None
            with contextlib.suppress(Exception):
                worker_exc = worker_task.exception()
            pytest.fail(
                f"Worker crashed after initial token expired: {worker_exc!r}. "
                "Token refresh did not keep the worker connected to Docker Temporal."
            )

        if completed_post_expiry and not any(completed_post_expiry):
            pytest.fail(
                "All workflow tasks submitted after token expiry failed to complete. "
                "Worker is alive but authentication is broken post-expiry."
            )

        assert token_svc._call_count >= 2, (
            f"Background refresh should have called get_token at least twice "
            f"(got {token_svc._call_count} calls)."
        )
    finally:
        submit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await submit_task
        await manager.shutdown()
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker_task


# ---------------------------------------------------------------------------
# Clock-skew reproduction
# ---------------------------------------------------------------------------

# Container clock constants for the skew test
_SKEW_TOKEN_TTL = 60  # OAuth server issues 60-second tokens (correct clock)
_CLOCK_SKEW_SECONDS = 60  # Simulate container clock 60 s behind real time

# At TOKEN_TTL/2 + 5 s the refresh should have fired if calibration is working:
#   Without fix: sleep = (TTL + SKEW)/2 = 60 s → no refresh yet at t=35 s
#   With fix:    sleep = TTL/2 = 30 s          → refresh has fired by t=35 s
_SKEW_OBSERVE_AT = _SKEW_TOKEN_TTL // 2 + 5  # 35 s


class _ServerClockMockTokenService:
    """Issues tokens using the real (server-correct) clock.

    Simulates an OAuth server whose wall clock is correct.  The container's
    own clock may be skewed, but the token's iat/exp claims are always set
    with real time — matching production behaviour.
    """

    def __init__(self, private_key: bytes) -> None:
        self._private_key = private_key
        self._call_count = 0
        self.current_expires_at: datetime | None = None

    async def get_token(self, *, force_refresh: bool = False) -> str:
        self._call_count += 1
        now_real = int(time.time())  # real clock — NOT affected by the patch
        exp = now_real + _SKEW_TOKEN_TTL
        self.current_expires_at = datetime.fromtimestamp(exp, tz=UTC)
        return _issue_rs256_jwt(self._private_key, _SKEW_TOKEN_TTL)


async def test_iat_clock_offset_corrects_skewed_refresh_timing(
    private_key: bytes,
) -> None:
    """TemporalAuthManager detects container clock skew via JWT iat and corrects
    the refresh schedule so the token is rotated before it expires server-side.

    Production failure mode
    -----------------------
    The SDR runs on the developer's macOS machine.  If the host sleeps, the
    Podman VM clock pauses while real time advances.  After wake the container
    clock can be minutes behind real time.  The OAuth server (correct clock)
    issues a token with, say, exp = real_now + 10 min.  But the container's
    _calculate_sleep_seconds computes:

        remaining = exp - slow_local_now = 10 min + skew

    It therefore schedules the next refresh too late — past the point where the
    Temporal server (correct clock) has already rejected the token.

    How the fix works
    -----------------
    After calling get_token(), _do_refresh records the local clock at receipt
    and reads the JWT's iat claim (set by the OAuth server at issue time):

        offset = iat - local_time_at_receipt  # positive when container is slow

    _calculate_sleep_seconds then adjusts:

        adjusted_now = local_now + offset  ≈ server_now

    and computes remaining = exp - adjusted_now = TTL (correct).

    Test setup
    ----------
    Token TTL: {_SKEW_TOKEN_TTL}s (OAuth server — real clock)
    Simulated skew: {_CLOCK_SKEW_SECONDS}s (container clock is this many seconds slow)
    Observe at: t = {_SKEW_OBSERVE_AT}s

    The patch covers both time.time and datetime.now in the auth module and
    includes acquire_initial_token, so _update_clock_offset sees the asymmetry
    on the very first token receipt:
        offset = iat (real) - slow_time.time() = +{_CLOCK_SKEW_SECONDS}s

    Without fix: offset not applied → remaining = TTL + SKEW = {_SKEW_TOKEN_TTL + _CLOCK_SKEW_SECONDS}s
                 sleep = max(30, min({(_SKEW_TOKEN_TTL + _CLOCK_SKEW_SECONDS) // 2}, 300)) = {min((_SKEW_TOKEN_TTL + _CLOCK_SKEW_SECONDS) // 2, 300)}s
                 → first refresh has NOT yet fired at t={_SKEW_OBSERVE_AT}s (call_count == 1)
    With fix:    offset applied → remaining = TTL = {_SKEW_TOKEN_TTL}s
                 sleep = max(30, min({_SKEW_TOKEN_TTL // 2}, 300)) = {max(30, min(_SKEW_TOKEN_TTL // 2, 300))}s
                 → first refresh HAS fired by t={_SKEW_OBSERVE_AT}s (call_count >= 2)
    """
    pk = private_key
    token_svc = _ServerClockMockTokenService(pk)

    manager = TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id="test",
            client_secret="test",
            token_url="http://localhost:0/unused",
        )
    )
    manager._token_service = token_svc

    # Patch both time.time and datetime.now inside the auth module to simulate
    # a container clock that is _CLOCK_SKEW_SECONDS behind real time.
    #
    # acquire_initial_token is called INSIDE the patch so that the very first
    # _update_clock_offset call sees: iat (real, from OAuth server) vs
    # time.time() (slow, patched) → offset = +_CLOCK_SKEW_SECONDS.
    #
    # _ServerClockMockTokenService.get_token uses the test-module's time.time
    # (unpatched), so iat/exp in issued tokens remain server-correct, matching
    # the production asymmetry: container clock drifted, OAuth server correct.
    real_time_time = time.time
    real_datetime_now = datetime.now

    def slow_time_time() -> float:
        return real_time_time() - _CLOCK_SKEW_SECONDS

    def slow_datetime_now(tz: object = None) -> datetime:
        return real_datetime_now(tz) - timedelta(seconds=_CLOCK_SKEW_SECONDS)  # type: ignore[arg-type]

    with (
        patch("application_sdk.execution._temporal.auth.time") as mock_time,
        patch("application_sdk.execution._temporal.auth.datetime") as mock_dt,
    ):
        mock_time.time.side_effect = slow_time_time
        mock_dt.now.side_effect = slow_datetime_now
        mock_dt.fromtimestamp = datetime.fromtimestamp

        initial_token = await manager.acquire_initial_token()
        client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=initial_token)
        manager.start_background_refresh(client)
        await asyncio.sleep(_SKEW_OBSERVE_AT)

    try:
        assert token_svc._call_count >= 2, (
            f"Token service called only {token_svc._call_count} time(s) after "
            f"{_SKEW_OBSERVE_AT}s. "
            f"Without clock-offset calibration the refresh sleeps "
            f"({_SKEW_TOKEN_TTL}+{_CLOCK_SKEW_SECONDS})/2 = "
            f"{(_SKEW_TOKEN_TTL + _CLOCK_SKEW_SECONDS) // 2}s, so it has not "
            f"fired yet. "
            "Fix: derive server clock offset from JWT iat in _do_refresh and "
            "apply it in _calculate_sleep_seconds."
        )
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------
# Mechanism probes — empirically characterise what the SDK actually does
# ---------------------------------------------------------------------------


async def test_api_key_assignment_propagates_to_worker_polls(
    private_key: bytes,
) -> None:
    """Prove that client.api_key is read on every poll, not cached at startup.

    A healthy worker is running with a long-lived token.  We then corrupt
    client.api_key with a garbage value — the same assignment that
    TemporalAuthManager._do_refresh uses — and submit a new workflow.

    If api_key is read per-poll the worker crashes with PermissionDenied.
    If the SDK cached the original token at connect time the worker keeps
    running undisturbed.

    The result tells us whether the TemporalAuthManager fix mechanism
    (client.api_key = new_token) can actually influence in-flight workers.
    """
    pk = private_key
    task_queue = f"docker-probe-propagate-{int(time.time())}"

    long_token = _issue_rs256_jwt(pk, 3600)
    client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=long_token)

    admin_token = _issue_rs256_jwt(pk, 3600, ["temporal-system:admin"])
    admin_client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=admin_token)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[_DockerAuthNoopWorkflow],
    )
    worker_task = asyncio.create_task(worker.run())

    try:
        # Confirm the worker is healthy: submit and complete a workflow.
        handle = await admin_client.start_workflow(
            _DockerAuthNoopWorkflow.run,
            id=f"probe-healthy-{int(time.time() * 1000)}",
            task_queue=task_queue,
        )
        await asyncio.wait_for(handle.result(), timeout=15.0)
        assert not worker_task.done(), "Worker crashed before api_key was corrupted"

        # Corrupt the api_key — same operation as TemporalAuthManager._do_refresh.
        client.api_key = "not-a-valid-jwt"

        # Submit another workflow so a new poll RPC is triggered immediately.
        await admin_client.start_workflow(
            _DockerAuthNoopWorkflow.run,
            id=f"probe-corrupt-{int(time.time() * 1000)}",
            task_queue=task_queue,
        )

        # Give the SDK up to 15 s to start a new poll with the corrupted key.
        deadline = asyncio.get_event_loop().time() + 15.0
        while asyncio.get_event_loop().time() < deadline:
            if worker_task.done():
                break
            await asyncio.sleep(0.5)

        if worker_task.done():
            worker_exc: BaseException | None = None
            with contextlib.suppress(Exception):
                worker_exc = worker_task.exception()
            rust_err = (
                worker_exc.__cause__
                if worker_exc and worker_exc.__cause__
                else worker_exc
            )
            assert rust_err is not None
            assert (
                "PermissionDenied" in str(rust_err)
                or "unauthorized" in str(rust_err).lower()
            ), f"Worker crashed but not with PermissionDenied: {rust_err}"
            # RESULT: api_key IS read per-poll — corruption propagated.
        else:
            # RESULT: api_key is NOT read per-poll — the SDK cached the
            # original token and the corruption had no effect.
            pytest.fail(
                "Worker did not crash after client.api_key was set to garbage. "
                "This means the SDK does NOT read client.api_key for each poll — "
                "it cached the original token at connect time. "
                "If true, TemporalAuthManager._do_refresh's client.api_key assignment "
                "has no effect on running workers."
            )
    finally:
        if not worker_task.done():
            worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker_task


async def test_api_key_update_rescues_worker_during_grace_period(
    private_key: bytes,
) -> None:
    """Prove that updating client.api_key mid-grace-period rescues the worker.

    A worker starts with a 5-second token.  No TemporalAuthManager is
    involved.  Once the token expires the SDK enters the 60-second
    LONG_POLL_FATAL_GRACE retry window.  Partway through that window we
    manually set client.api_key to a fresh valid token — exactly what
    TemporalAuthManager._do_refresh does — and submit a workflow.

    If the worker recovers: updating api_key mid-grace-period works.
    If the worker still crashes: the assignment does not help once polls
    are already failing, and the DISR-508 fix strategy is flawed.
    """
    pk = private_key
    task_queue = f"docker-probe-rescue-{int(time.time())}"

    short_token = _issue_rs256_jwt(pk, _INITIAL_TOKEN_TTL_SECONDS)
    client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=short_token)

    admin_token = _issue_rs256_jwt(pk, 3600, ["temporal-system:admin"])
    admin_client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=admin_token)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[_DockerAuthNoopWorkflow],
    )
    worker_task = asyncio.create_task(worker.run())

    try:
        # Wait for the token to expire and the grace period to begin.
        await asyncio.sleep(_INITIAL_TOKEN_TTL_SECONDS + 5)
        assert not worker_task.done(), (
            "Worker crashed before we could inject the fresh token — "
            "check that the grace period is still 60 s."
        )

        # Inject a fresh valid token mid-grace-period.
        fresh_token = _issue_rs256_jwt(pk, 3600)
        client.api_key = fresh_token

        # Submit a workflow to force a new poll RPC with the updated api_key.
        await admin_client.start_workflow(
            _DockerAuthNoopWorkflow.run,
            id=f"probe-rescue-{int(time.time() * 1000)}",
            task_queue=task_queue,
        )

        # Allow up to 20 s for the worker to either recover or crash.
        deadline = asyncio.get_event_loop().time() + 20.0
        recovered = False
        while asyncio.get_event_loop().time() < deadline:
            if worker_task.done():
                break
            await asyncio.sleep(0.5)
        else:
            recovered = True

        if recovered:
            # RESULT: worker is still alive — the api_key update rescued it.
            pass
        else:
            worker_exc_2: BaseException | None = None
            with contextlib.suppress(Exception):
                worker_exc_2 = worker_task.exception()
            pytest.fail(
                f"Worker crashed even after client.api_key was updated to a fresh "
                f"valid token mid-grace-period: {worker_exc_2!r}. "
                "This means updating client.api_key does NOT rescue a worker once "
                "polls are already failing — the DISR-508 fix strategy is insufficient."
            )
    finally:
        if not worker_task.done():
            worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker_task


async def test_real_manager_timing_with_short_lived_token(
    private_key: bytes,
) -> None:
    """Observe real TemporalAuthManager behaviour with no overrides.

    Uses a 5-second token and the unmodified _calculate_sleep_seconds logic.
    For a 5-second token, remaining/2 = 2.5 s, which is below the 30-second
    minimum floor, so the first refresh fires at t=30 — 25 seconds AFTER
    the token expires.

    The SDK's LONG_POLL_FATAL_GRACE is 60 s, so the grace window runs from
    t≈5 to t≈65.  The question is: does the refresh at t=30 (within the
    grace window) actually prevent the crash?

    PASSES: worker is alive at t=40 (refresh fired, worker recovered).
    FAILS:  worker crashed before t=40, meaning the real timing is wrong.
    """
    pk = private_key
    task_queue = f"docker-probe-timing-{int(time.time())}"

    token_svc = _Rs256MockTokenService(pk)
    manager = TemporalAuthManager(
        config=TemporalAuthConfig(
            client_id="test",
            client_secret="test",
            token_url="http://localhost:0/unused",
        )
    )
    manager._token_service = token_svc
    # No override of _calculate_sleep_seconds — real SDK logic only.

    initial_token = await manager.acquire_initial_token()
    client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=initial_token)

    admin_token = _issue_rs256_jwt(pk, 3600, ["temporal-system:admin"])
    admin_client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=admin_token)

    manager.start_background_refresh(client)

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[_DockerAuthNoopWorkflow],
    )
    worker_task = asyncio.create_task(worker.run())

    try:
        # Submit workflows every second to keep polls cycling so we see
        # the effect of the token update as soon as the refresh fires.
        async def _submit() -> None:
            for i in range(45):
                if worker_task.done():
                    break
                try:
                    await admin_client.start_workflow(
                        _DockerAuthNoopWorkflow.run,
                        id=f"probe-timing-{int(time.time() * 1000)}-{i}",
                        task_queue=task_queue,
                    )
                except Exception:  # noqa: BLE001,S110
                    pass
                await asyncio.sleep(1.0)

        submit_task = asyncio.create_task(_submit())
        try:
            # Real timing: token TTL=5s, sleep=30s, refresh fires at t≈30.
            # We observe at t=40 — after the refresh should have fired.
            await asyncio.sleep(40)

            if worker_task.done():
                worker_exc_3: BaseException | None = None
                with contextlib.suppress(Exception):
                    worker_exc_3 = worker_task.exception()
                pytest.fail(
                    f"Worker crashed at t≈40 despite real TemporalAuthManager running. "
                    f"The refresh fires at t=30 (30s floor) but the worker crashed "
                    f"before t=40. Exception: {worker_exc_3!r}. "
                    f"Token service call count: {token_svc._call_count}."
                )

            assert token_svc._call_count >= 2, (
                f"Refresh should have fired by t=40 (call_count={token_svc._call_count}). "
                "Real _calculate_sleep_seconds may not be firing as expected."
            )
        finally:
            submit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await submit_task
    finally:
        await manager.shutdown()
        if not worker_task.done():
            worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker_task


# ---------------------------------------------------------------------------
# Exact production error reproducer
# ---------------------------------------------------------------------------

# SDK retry constants (from sdk-core/crates/client/src/retry.rs):
#   LONG_POLL_FATAL_GRACE = 60s  — PermissionDenied on a task poll is retried
#                                   for 60 s before becoming fatal.
#   task_poll_retry_policy: initial=200ms, multiplier=2, max_interval=10s
#
# Timeline: token expires → first PermissionDenied → ~60 s of retries →
#           RuntimeError propagates → worker crashes.
# Total test runtime: _INITIAL_TOKEN_TTL_SECONDS + ~60 s ≈ 70 s.
_LONG_POLL_FATAL_GRACE_SECONDS = 60


async def test_worker_crashes_with_exact_production_error(
    private_key: bytes,
) -> None:
    """Reproduces the exact RuntimeError seen in production when a JWT expires
    with no token refresh in place.

    Expected error (matches production logs verbatim):

        RuntimeError: Poll failure: Unhandled grpc error when polling:
        Status { code: PermissionDenied, message: "Request unauthorized.",
        details: ... type.googleapis.com/temporal.api.errordetails.v1.PermissionDeniedFailure }

    This goes through the real production code path:
      - Real Docker Temporal with RS256 JWT auth enforcement
      - Real Temporal Python worker making real PollWorkflowTaskQueue /
        PollActivityTaskQueue RPCs via the Rust SDK core bridge
      - No TemporalAuthManager — token is never refreshed
      - Workflows submitted every second so polls return quickly after
        expiry, triggering new polls that carry the now-expired token

    The SDK retries PermissionDenied for LONG_POLL_FATAL_GRACE (60 s) before
    forwarding the error as fatal — so this test runs for ~70 s by design.

    PASSES: worker crashes with the exact RuntimeError (confirms the error is
            real and the production path is exercised).
    FAILS:  worker does not crash within the expected window, meaning either
            auth enforcement is broken or the grace period behaviour changed.
    """
    pk = private_key
    task_queue = f"docker-crash-{int(time.time())}"

    token = _issue_rs256_jwt(pk, _INITIAL_TOKEN_TTL_SECONDS, [f"{_NAMESPACE}:write"])
    worker_client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=token)

    admin_token = _issue_rs256_jwt(pk, 3600, ["temporal-system:admin"])
    admin_client = await Client.connect(_TEMPORAL_HOST, tls=False, api_key=admin_token)

    worker = Worker(
        worker_client,
        task_queue=task_queue,
        workflows=[_DockerAuthNoopWorkflow],
    )
    worker_task = asyncio.create_task(worker.run())

    # Submit a workflow every second so in-flight polls return quickly,
    # ensuring the worker starts new polls (carrying the expired token)
    # promptly after expiry rather than waiting up to 70 s for long-poll timeout.
    async def _submit_loop() -> None:
        for i in range(
            _INITIAL_TOKEN_TTL_SECONDS + _LONG_POLL_FATAL_GRACE_SECONDS + 10
        ):
            if worker_task.done():
                break
            try:
                await admin_client.start_workflow(
                    _DockerAuthNoopWorkflow.run,
                    id=f"crash-{int(time.time() * 1000)}-{i}",
                    task_queue=task_queue,
                )
            except Exception:  # noqa: BLE001,S110
                pass
            await asyncio.sleep(1.0)

    submit_task = asyncio.create_task(_submit_loop())

    # Give enough time for: startup + token TTL + full grace period + buffer.
    wait_seconds = _INITIAL_TOKEN_TTL_SECONDS + _LONG_POLL_FATAL_GRACE_SECONDS + 10

    try:
        deadline = asyncio.get_event_loop().time() + wait_seconds
        while asyncio.get_event_loop().time() < deadline:
            if worker_task.done():
                break
            await asyncio.sleep(1.0)

        assert worker_task.done(), (
            f"Worker should have crashed within {wait_seconds}s "
            f"(token TTL={_INITIAL_TOKEN_TTL_SECONDS}s + "
            f"grace={_LONG_POLL_FATAL_GRACE_SECONDS}s + buffer) "
            "but is still running. Auth may not be enforced on poll RPCs."
        )

        worker_exc: BaseException | None = None
        with contextlib.suppress(Exception):
            worker_exc = worker_task.exception()

        assert worker_exc is not None, "Worker task finished without an exception"
        assert isinstance(
            worker_exc, RuntimeError
        ), f"Expected RuntimeError, got {type(worker_exc).__name__}: {worker_exc}"

        # _workflow.py wraps the Rust bridge error:
        #   raise RuntimeError("Workflow worker failed") from <rust_error>
        # The original Poll failure is in __cause__.
        rust_err = worker_exc.__cause__ or worker_exc
        err = str(rust_err)
        assert "Poll failure: Unhandled grpc error when polling:" in err, err
        assert "PermissionDenied" in err, err
        assert "Request unauthorized." in err, err
        assert "PermissionDeniedFailure" in err, err

    finally:
        submit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await submit_task
        if not worker_task.done():
            worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await worker_task
