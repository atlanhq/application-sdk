"""On the consolidated host, one app's stuck ``run_in_thread`` calls must not
stall another app.

The worker runs one app per process and shares one blocking pool; a pod whose
pool is exhausted is restarted. The host serves many apps from one process, so
``build_asgi_app`` gives each app its own pool.
"""

from __future__ import annotations

import asyncio
import threading

import httpx

from application_sdk._runtime.offload import run_in_thread
from application_sdk.handler.asgi import build_asgi_app
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import AuthInput, AuthOutput, AuthStatus

_RELEASE = threading.Event()
_THREADS: dict[str, str] = {}


class _Base(Handler):
    async def preflight_check(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("not called")

    async def fetch_metadata(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("not called")


class _Stuck(_Base):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        _THREADS["stuck"] = await run_in_thread(
            lambda: (_RELEASE.wait(10), threading.current_thread().name)[1]
        )
        return AuthOutput(status=AuthStatus.SUCCESS)


class _Healthy(_Base):
    async def test_auth(self, input: AuthInput) -> AuthOutput:
        _THREADS["healthy"] = await run_in_thread(
            lambda: threading.current_thread().name
        )
        return AuthOutput(status=AuthStatus.SUCCESS)


async def test_a_stuck_app_does_not_stall_another_app() -> None:
    stuck = build_asgi_app(_Stuck(), app_name="stuck", blocking_threads=1)
    healthy = build_asgi_app(_Healthy(), app_name="healthy", blocking_threads=1)
    body = {"credentials": []}
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=stuck), base_url="http://s"
            ) as s,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=healthy), base_url="http://h"
            ) as h,
        ):
            blocked = asyncio.create_task(s.post("/workflows/v1/auth", json=body))
            await asyncio.sleep(0.1)  # its only thread is now stuck
            response = await asyncio.wait_for(
                h.post("/workflows/v1/auth", json=body), timeout=5
            )
            assert response.status_code == 200
            assert _THREADS["healthy"].startswith("api-healthy-")
            _RELEASE.set()
            assert (await blocked).status_code == 200
            assert _THREADS["stuck"].startswith("api-stuck-")
    finally:
        _RELEASE.set()
        stuck.state.blocking_executor.shutdown(wait=False)
        healthy.state.blocking_executor.shutdown(wait=False)


async def test_outside_a_hosted_request_run_in_thread_uses_the_shared_pool() -> None:
    name = await run_in_thread(lambda: threading.current_thread().name)
    assert name.startswith("sdk-blocking-")


async def test_submit_in_thread_uses_the_apps_pool_too() -> None:
    import concurrent.futures

    from application_sdk._runtime.offload import blocking_executor, submit_in_thread

    pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="api-solo-")
    try:
        with blocking_executor(pool):
            future = submit_in_thread(lambda: threading.current_thread().name)
        assert future.result(timeout=5).startswith("api-solo-")
    finally:
        pool.shutdown(wait=True)
