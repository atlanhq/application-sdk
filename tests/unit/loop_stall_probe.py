"""Run ``run_worker_mode`` in its own process and send it a real SIGTERM.

``python -m tests.unit.loop_stall_probe <block> --when ... --sigterm-after S
[--drain S] [--linger S]``

The stall watchdog's job is to call ``_exit(1)`` on the whole process, which a
test cannot let happen inside the pytest worker, so ``test_loop_stall_watchdog.py``
runs this module as a child and reads its exit code and stderr. Temporal, Dapr
and the health server are mocked. The run mode, its signal handlers (asyncio's
and the watchdog's), the watchdog, faulthandler and the exit are real, and the
bound comes from ``ATLAN_WORKER_LOOP_STALL_TIMEOUT_SECONDS``. POSIX only:
asyncio has no signal handlers on Windows.

Arguments:
    block: What the event loop's thread does: ``none``, ``sleep`` (an untimed
        ``time.sleep``), ``lock`` (a ``threading.Lock`` acquired twice) or
        ``sleep:N`` (blocked for N seconds, then free again).
    --when: ``steady`` or ``before-sigterm``: half a second after the worker
        starts. ``during-drain``: half a second into the drain.
    --sigterm-after: Seconds after the worker starts until a thread sends
        SIGTERM. Counted from the worker's start, not the process's, because
        ``run_worker_mode`` installs its signal handlers only after startup
        setup: a SIGTERM before then would hit the default disposition on a
        slow CI runner and kill the probe without exercising the watchdog.
    --drain: Seconds the worker's drain takes, with the loop running.
    --linger: Seconds to stay alive after ``run_worker_mode`` returns, so a
        timer left armed by a clean shutdown would still fire.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

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


class _Worker:
    """Stands in for the Temporal worker: entering it is the worker starting,
    leaving it is the drain."""

    def __init__(
        self, block: str, when: str, drain: float, sigterm: threading.Timer
    ) -> None:
        self._blocker = _blocker(block)
        self._when = when
        self._drain = drain
        self._sigterm = sigterm

    async def __aenter__(self) -> _Worker:
        # run_worker_mode installs both SIGTERM handlers before it enters the
        # worker, so the countdown starts here. A supervised restart re-enters
        # the worker; the timer starts only once.
        if self._sigterm.ident is None:
            self._sigterm.start()
        if self._blocker is not None and self._when != "during-drain":
            asyncio.get_running_loop().call_later(BLOCK_AFTER_SECONDS, self._blocker)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._when == "during-drain" and self._blocker is not None:
            await asyncio.sleep(BLOCK_AFTER_SECONDS)
            self._blocker()
        await asyncio.sleep(self._drain)


def _async_cm() -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("block")
    parser.add_argument(
        "--when", choices=("steady", "before-sigterm", "during-drain"), required=True
    )
    parser.add_argument("--sigterm-after", type=float, required=True)
    parser.add_argument("--drain", type=float, default=0)
    parser.add_argument("--linger", type=float, default=0)
    args = parser.parse_args(argv)

    app_class = MagicMock()
    app_class._app_name = "probe-app"
    app_class._app_version = "1.0.0"

    # A thread, so the signal is sent even while the loop is blocked. Started
    # by the fake worker once the signal handlers are in place.
    sigterm = threading.Timer(
        args.sigterm_after, os.kill, (os.getpid(), signal.SIGTERM)
    )
    sigterm.daemon = True

    with (
        patch(
            "application_sdk.main._create_infrastructure",
            new_callable=AsyncMock,
            return_value=MagicMock(),
        ),
        patch("application_sdk.main.load_app_class", return_value=app_class),
        patch("application_sdk.main.validate_app_class"),
        patch("application_sdk.main.load_handler_class", return_value=None),
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
            side_effect=lambda *_, **__: _Worker(
                args.block, args.when, args.drain, sigterm
            ),
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
    time.sleep(args.linger)
    sys.stdout.write("probe exited cleanly\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
