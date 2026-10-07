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
    _RELEASE.clear()
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


async def test_several_stuck_apps_do_not_stall_another_app() -> None:
    """Each stuck app fills only its own pool, however many of them there are."""
    _RELEASE.clear()
    stuck = [
        build_asgi_app(_Stuck(), app_name=f"stuck-{i}", blocking_threads=1)
        for i in range(4)
    ]
    healthy = build_asgi_app(_Healthy(), app_name="healthy", blocking_threads=1)
    body = {"credentials": []}
    clients = [
        httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://s")
        for a in stuck
    ]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=healthy), base_url="http://h"
        ) as h:
            blocked = [
                asyncio.create_task(c.post("/workflows/v1/auth", json=body))
                for c in clients
            ]
            await asyncio.sleep(0.1)  # every stuck app's only thread is now stuck
            response = await asyncio.wait_for(
                h.post("/workflows/v1/auth", json=body), timeout=5
            )
            assert response.status_code == 200
            assert _THREADS["healthy"].startswith("api-healthy-")
            _RELEASE.set()
            for task in blocked:
                assert (await task).status_code == 200
    finally:
        _RELEASE.set()
        for c in clients:
            await c.aclose()
        for a in [*stuck, healthy]:
            a.state.blocking_executor.shutdown(wait=False)


async def test_a_hosted_request_is_charged_to_its_app() -> None:
    """Every route, not only the handler routes, charges its offloads to its app."""
    from application_sdk._runtime import offload

    app = build_asgi_app(_Healthy(), app_name="charged", blocking_threads=1)

    @app.get("/owner")
    async def owner() -> dict[str, str]:
        return {"owner": offload._current_offload_owner()}

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://c"
        ) as c:
            response = await c.get("/owner")
        assert response.json() == {"owner": "charged"}
    finally:
        app.state.blocking_executor.shutdown(wait=False)


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
