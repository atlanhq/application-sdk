"""Run ``run_worker_mode`` in its own process so the stall watchdog can end it.

``python -m tests.unit.loop_stall_probe <block> <shutdown_after> <linger>
[--phase worker|activity|after-activity] [--heartbeat SECONDS]``

The watchdog's job is to call ``_exit(1)`` on the whole process, which a test
cannot let happen inside the pytest worker, so ``test_loop_stall_watchdog.py``
runs this module as a child and reads its exit code and stderr. Temporal, Dapr
and the health server are mocked; the run mode, the watchdog, faulthandler and
the exit are real, and the timeout comes from
``ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS`` as in a deployed worker.

Arguments:
    block: What the event loop's thread does: ``none``, ``sleep`` (an untimed
        ``time.sleep``), ``lock`` (a ``threading.Lock`` acquired twice) or
        ``sleep:N`` (blocked for N seconds, then free again).
    shutdown_after: Seconds until the shutdown signal handler runs.
    linger: Seconds to stay alive after ``run_worker_mode`` returns, so a timer
        left armed by a clean shutdown would still fire.
    --phase: Where the block happens. ``worker`` (default): half a second after
        the worker starts, with no activity running. ``activity``: inside an
        activity body. ``after-activity``: once that activity has finished.
    --heartbeat: The activity's heartbeat timeout in seconds; 0 for none. The
        activity runs through the SDK's real ``LoopStallInterceptor`` in a real
        temporalio activity context, so the watchdog sees it as in a worker.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from temporalio.testing import ActivityEnvironment
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput

from application_sdk.execution._temporal.interceptors.loop_stall import (
    LoopStallInterceptor,
)
from application_sdk.main import AppConfig, run_worker_mode

BLOCK_AFTER_SECONDS = 0.5


def _block_on_sleep() -> None:
    time.sleep(3600)


def _block_on_lock() -> None:
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()


def _blocker(block: str) -> Callable[[], None] | None:
    if block == "none":
        return None
    if block == "sleep":
        return _block_on_sleep
    if block == "lock":
        return _block_on_lock
    seconds = float(block.removeprefix("sleep:"))

    def _block_briefly() -> None:
        time.sleep(seconds)

    return _block_briefly


class _ActivityBody(ActivityInboundInterceptor):
    """The innermost link of the chain: the activity itself."""

    def __init__(self, blocker: Callable[[], None] | None) -> None:
        self._blocker = blocker

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        if self._blocker is not None:
            self._blocker()
        return None


async def _run_activity(
    heartbeat: float, blocker_inside: Callable[[], None] | None
) -> None:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info,
        heartbeat_timeout=timedelta(seconds=heartbeat) if heartbeat else None,
    )
    chain = LoopStallInterceptor().intercept_activity(_ActivityBody(blocker_inside))
    activity_input = ExecuteActivityInput(
        fn=_run_activity, args=(), executor=None, headers={}
    )

    async def _execute() -> None:
        await chain.execute_activity(activity_input)

    await env.run(_execute)


class _Worker:
    """Stands in for the Temporal worker; entering it starts the scenario."""

    def __init__(self, block: str, phase: str, heartbeat: float) -> None:
        self._blocker = _blocker(block)
        self._phase = phase
        self._heartbeat = heartbeat
        self._activity: asyncio.Future[None] | None = None

    async def __aenter__(self) -> _Worker:
        if self._phase == "worker":
            if self._blocker is not None:
                asyncio.get_running_loop().call_later(
                    BLOCK_AFTER_SECONDS, self._blocker
                )
        else:
            self._activity = asyncio.ensure_future(self._activity_then_block())
        return self

    async def _activity_then_block(self) -> None:
        await asyncio.sleep(BLOCK_AFTER_SECONDS)
        inside = self._blocker if self._phase == "activity" else None
        await _run_activity(self._heartbeat, inside)
        if self._phase == "after-activity" and self._blocker is not None:
            self._blocker()

    async def __aexit__(self, *exc: object) -> None:
        return None


def _async_cm() -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("block")
    parser.add_argument("shutdown_after", type=float)
    parser.add_argument("linger", type=float)
    parser.add_argument(
        "--phase", choices=("worker", "activity", "after-activity"), default="worker"
    )
    parser.add_argument("--heartbeat", type=float, default=0)
    args = parser.parse_args(argv)
    block, shutdown_after, linger = args.block, args.shutdown_after, args.linger

    app_class = MagicMock()
    app_class._app_name = "probe-app"
    app_class._app_version = "1.0.0"

    def _signal_after_delay(loop: asyncio.AbstractEventLoop, handler: Any) -> None:
        # Stands in for SIGTERM without depending on platform signal support.
        loop.call_later(shutdown_after, handler)

    with (
        patch(
            "application_sdk.main._create_infrastructure",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
        patch("application_sdk.main.load_app_class", return_value=app_class),
        patch("application_sdk.main.validate_app_class"),
        patch("application_sdk.main.load_handler_class", return_value=None),
        patch(
            "application_sdk.main._install_graceful_signal_handlers",
            side_effect=_signal_after_delay,
        ),
        patch("application_sdk.main._flush_observability", new_callable=AsyncMock),
        patch(
            "application_sdk.main.restart_marker.wait_if_pod_restarted",
            new_callable=AsyncMock,
        ),
        patch("application_sdk.main.restart_marker.clear"),
        patch(
            "application_sdk.execution._temporal.backend.create_temporal_client",
            new_callable=AsyncMock,
        ),
        patch(
            "application_sdk.execution._temporal.converter.create_data_converter_for_app"
        ),
        patch(
            "application_sdk.execution._temporal.worker.create_worker",
            side_effect=lambda *_, **__: _Worker(block, args.phase, args.heartbeat),
        ),
        patch(
            "application_sdk.server.health.WorkerHealthServer",
            return_value=_async_cm(),
        ),
        patch(
            "application_sdk.infrastructure.context.close_infrastructure",
            new_callable=AsyncMock,
        ),
    ):
        asyncio.run(
            run_worker_mode(
                AppConfig(mode="worker", app_module="probe_pkg:ProbeApp"),
                bound_post_drain_exit=True,
            )
        )
    time.sleep(linger)
    sys.stdout.write("probe exited cleanly\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
