"""Run ``run_worker_mode`` in its own process so the stall watchdog can end it.

``python -m tests.unit.loop_stall_probe <block> <shutdown_after> <linger>``

The watchdog's job is to call ``_exit(1)`` on the whole process, which a test
cannot let happen inside the pytest worker, so ``test_loop_stall_watchdog.py``
runs this module as a child and reads its exit code and stderr. Temporal, Dapr
and the health server are mocked; the run mode, the watchdog, faulthandler and
the exit are real, and the timeout comes from
``ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS`` as in a deployed worker.

Arguments:
    block: What the running worker does to the event loop half a second in:
        ``none``, ``sleep`` (an untimed ``time.sleep``) or ``lock`` (a
        ``threading.Lock`` acquired twice).
    shutdown_after: Seconds until the shutdown signal handler runs.
    linger: Seconds to stay alive after ``run_worker_mode`` returns, so a timer
        left armed by a clean shutdown would still fire.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from application_sdk.main import AppConfig, run_worker_mode

BLOCK_AFTER_SECONDS = 0.5


def _block_on_sleep() -> None:
    time.sleep(3600)


def _block_on_lock() -> None:
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()


_BLOCKERS = {"none": None, "sleep": _block_on_sleep, "lock": _block_on_lock}


class _Worker:
    """Stands in for the Temporal worker; entering it starts the scenario."""

    def __init__(self, block: str) -> None:
        self._blocker = _BLOCKERS[block]

    async def __aenter__(self) -> _Worker:
        if self._blocker is not None:
            asyncio.get_running_loop().call_later(BLOCK_AFTER_SECONDS, self._blocker)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _async_cm() -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def main(argv: list[str]) -> None:
    block, shutdown_after, linger = argv[0], float(argv[1]), float(argv[2])

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
            side_effect=lambda *_, **__: _Worker(block),
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
