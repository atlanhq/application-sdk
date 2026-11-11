"""Per-app containment of the ``sdk-blocking`` pool (FND-2973).

Real threads throughout: what is under test is how many pool slots an app's
hung calls hold, and whether another app's calls still get through, and neither
exists without one. Every blocked call waits on an event with a release
deadline, so a regression fails the test instead of hanging the suite.

Each test charges its calls to its own app name, so the process-wide ledger
never carries one test's counts into another.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from application_sdk._runtime import offload
from application_sdk._runtime.offload import (
    OFFLOAD_MAX_THREADS_PER_APP_ENV,
    blocking_executor,
    offload_owner,
    run_in_thread,
)
from application_sdk.errors import FailureCategory, ResourceExhaustedError
from application_sdk.handler.context import HandlerContext, bind_handler_context

#: How long a blocked call waits before releasing itself.
_RELEASE_AFTER = 5.0


class _Hang:
    """A blocking call that runs until released, like a driver stuck on a dead host."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        #: Set only when the call ends: a caller that came back while it is
        #: unset did not wait for this call.
        self.returned = threading.Event()

    def __call__(self) -> str:
        self.started.set()
        self.release.wait(_RELEASE_AFTER)
        self.returned.set()
        return "released"


async def _wait_until(predicate, what: str) -> None:
    deadline = time.monotonic() + _RELEASE_AFTER
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.01)


async def _strand(app: str, hang: _Hang) -> None:
    """Start *hang* for *app*, then give up on it the way a deadline does."""
    with offload_owner(app):
        task = asyncio.create_task(run_in_thread(hang))
    await _wait_until(hang.started.is_set, "the hung call to start")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.fixture
def hangs() -> Iterator[list[_Hang]]:
    """Hung calls created by a test, all released when it ends."""
    created: list[_Hang] = []
    yield created
    for hang in created:
        hang.release.set()


@pytest.fixture
def cap(monkeypatch: pytest.MonkeyPatch):
    def _set(value: int | str) -> None:
        monkeypatch.setenv(OFFLOAD_MAX_THREADS_PER_APP_ENV, str(value))

    return _set


class TestPerAppCap:
    async def test_one_apps_hung_calls_do_not_block_another_app(
        self, cap, hangs: list[_Hang]
    ) -> None:
        """The FND-2973 acceptance shape: app A hangs, app B still runs promptly."""
        cap(2)
        for _ in range(2):
            hang = _Hang()
            hangs.append(hang)
            await _strand("app-a-blocks", hang)

        with offload_owner("app-a-blocks"):
            with pytest.raises(ResourceExhaustedError) as refused:
                await run_in_thread(lambda: "never runs")

        with offload_owner("app-b-unaffected"):
            result = await asyncio.wait_for(
                run_in_thread(lambda: "b ran"), _RELEASE_AFTER
            )
        assert result == "b ran"
        assert not any(h.returned.is_set() for h in hangs), "b waited for a's calls"

        error = refused.value
        assert error.category is FailureCategory.RESOURCE_EXHAUSTED
        assert error.effective_retryable is True
        assert error.resource == "offload_threads"
        assert error.limit == "2"
        assert "app-a-blocks" in error.message

    async def test_a_refusal_is_immediate_not_queued(
        self, cap, hangs: list[_Hang]
    ) -> None:
        cap(1)
        hang = _Hang()
        hangs.append(hang)
        await _strand("app-refused-fast", hang)

        with offload_owner("app-refused-fast"):
            with pytest.raises(ResourceExhaustedError):
                await asyncio.wait_for(run_in_thread(lambda: None), _RELEASE_AFTER)
        assert not hang.returned.is_set(), "the refusal queued behind the hung call"

    async def test_the_slot_is_held_until_the_thread_finishes_not_the_caller(
        self, cap, hangs: list[_Hang]
    ) -> None:
        """A caller giving up must not hand back a slot its thread still occupies."""
        cap(1)
        hang = _Hang()
        hangs.append(hang)
        await _strand("app-slot-held", hang)

        with offload_owner("app-slot-held"):
            with pytest.raises(ResourceExhaustedError):
                await run_in_thread(lambda: None)

        hang.release.set()
        await _wait_until(
            lambda: offload._LEDGER.in_flight("app-slot-held") == 0,
            "the hung thread to return its slot",
        )
        with offload_owner("app-slot-held"):
            assert await run_in_thread(lambda: "free again") == "free again"

    async def test_no_cap_by_default_calls_beyond_the_pool_width_still_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset, nothing changes: a fan-out wider than the pool queues and completes."""
        monkeypatch.delenv(OFFLOAD_MAX_THREADS_PER_APP_ENV, raising=False)
        width = offload._BLOCKING_EXECUTOR._max_workers

        def brief() -> int:
            time.sleep(0.01)
            return 1

        with offload_owner("app-uncapped"):
            results = await asyncio.gather(
                *(run_in_thread(brief) for _ in range(width + 5))
            )
        assert sum(results) == width + 5
        assert offload._LEDGER.in_flight("app-uncapped") == 0

    async def test_an_invalid_cap_is_ignored_and_reported_once(
        self, cap, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap("not-a-number")
        monkeypatch.setattr(offload, "_REPORTED_BAD_CAPS", set())

        with patch.object(offload.logger, "warning") as warning:
            with offload_owner("app-bad-cap"):
                for _ in range(3):
                    assert await run_in_thread(lambda: "ran") == "ran"

        reports = [
            c
            for c in warning.call_args_list
            if OFFLOAD_MAX_THREADS_PER_APP_ENV in str(c)
        ]
        assert len(reports) == 1


class TestStrandedCount:
    async def test_a_hung_call_past_its_caller_is_counted_until_it_returns(
        self, hangs: list[_Hang]
    ) -> None:
        hang = _Hang()
        hangs.append(hang)
        with patch.object(offload.logger, "warning") as warning:
            await _strand("app-stranded", hang)

        assert offload._LEDGER.stranded().get("app-stranded") == 1
        warning.assert_called_once()
        rendered = str(warning.call_args)
        assert "outlived its caller" in rendered
        assert "app-stranded" in rendered
        assert "_Hang" in rendered

        with patch.object(offload.logger, "info") as info:
            hang.release.set()
            await _wait_until(
                lambda: "app-stranded" not in offload._LEDGER.stranded(),
                "the stranded count to clear",
            )
        finished = [c for c in info.call_args_list if "app-stranded" in str(c)]
        assert len(finished) == 1
        assert "finished" in str(finished[0])

    async def test_the_gauge_reports_stranded_threads_per_app(
        self, hangs: list[_Hang], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first strand registers the named gauge, and its callback reads the ledger."""
        monkeypatch.setattr(offload, "_INSTRUMENTS", {})
        hang = _Hang()
        hangs.append(hang)
        with patch("opentelemetry.metrics.get_meter") as get_meter:
            await _strand("app-gauge", hang)

        create = get_meter.return_value.create_observable_gauge
        create.assert_called_once()
        assert create.call_args.args[0] == "offload.stranded_threads"
        (callback,) = create.call_args.kwargs["callbacks"]
        observed = {o.attributes["app.name"]: o.value for o in callback(None)}
        assert observed.get("app-gauge") == 1

    async def test_a_queued_call_given_up_on_never_runs_and_leaks_nothing(
        self, cap
    ) -> None:
        """Cancelled while queued behind a full pool: cancelled, not stranded."""
        cap(10_000)
        release = threading.Event()
        width = offload._BLOCKING_EXECUTOR._max_workers
        started = threading.Barrier(width + 1)

        def occupy() -> None:
            started.wait(_RELEASE_AFTER)
            release.wait(_RELEASE_AFTER)

        blockers = [offload._BLOCKING_EXECUTOR.submit(occupy) for _ in range(width)]
        try:
            started.wait(_RELEASE_AFTER)
            ran = threading.Event()
            with offload_owner("app-queued"):
                task = asyncio.create_task(run_in_thread(ran.set))
            await _wait_until(
                lambda: offload._LEDGER.in_flight("app-queued") == 1,
                "the queued call to claim its slot",
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await _wait_until(
                lambda: offload._LEDGER.in_flight("app-queued") == 0,
                "the cancelled queued call to release its slot",
            )
            assert "app-queued" not in offload._LEDGER.stranded()
        finally:
            release.set()
            for blocker in blockers:
                blocker.result(_RELEASE_AFTER)
        await asyncio.sleep(0.05)
        assert not ran.is_set(), "a call cancelled while queued still ran"


class TestOnAnAppsOwnPool:
    """The consolidated host runs each app's calls on its own pool."""

    async def test_a_stranded_call_runs_on_the_apps_pool_and_is_counted(
        self, cap, hangs: list[_Hang]
    ) -> None:
        cap(1)
        pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="api-own-")
        hang = _Hang()
        hangs.append(hang)
        thread_name: list[str] = []

        def hung() -> str:
            thread_name.append(threading.current_thread().name)
            return hang()

        try:
            with blocking_executor(pool):
                with offload_owner("app-own-pool"):
                    task = asyncio.create_task(run_in_thread(hung))
                await _wait_until(hang.started.is_set, "the hung call to start")
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert thread_name[0].startswith("api-own-")
                assert offload._LEDGER.stranded().get("app-own-pool") == 1
                with offload_owner("app-own-pool"):
                    with pytest.raises(ResourceExhaustedError):
                        await run_in_thread(lambda: None)
            hang.release.set()
            await _wait_until(
                lambda: offload._LEDGER.in_flight("app-own-pool") == 0,
                "the stranded call to release its slot",
            )
        finally:
            hang.release.set()
            pool.shutdown(wait=True)


class TestOwnership:
    async def test_a_handler_invocation_charges_its_own_app(
        self, cap, hangs: list[_Hang]
    ) -> None:
        cap(1)
        hang = _Hang()
        hangs.append(hang)
        ctx = HandlerContext(app_name="app-from-handler")
        with bind_handler_context(ctx):
            task = asyncio.create_task(run_in_thread(hang))
        await _wait_until(hang.started.is_set, "the handler's call to start")
        try:
            assert offload._LEDGER.in_flight("app-from-handler") == 1
            with bind_handler_context(ctx):
                with pytest.raises(ResourceExhaustedError):
                    await run_in_thread(lambda: None)
        finally:
            hang.release.set()
            assert await task == "released"

    def test_unbound_calls_fall_back_to_the_process_app(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "app-process")
        assert offload._current_offload_owner() == "app-process"
        with offload_owner("app-bound"):
            assert offload._current_offload_owner() == "app-bound"
            with offload_owner(""):
                # An empty name does not mask the fallback.
                assert offload._current_offload_owner() == "app-process"

    def test_the_fallback_is_read_when_the_call_is_made(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not frozen at import: a host that sets its name late is still named."""
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        assert offload._current_offload_owner() == offload._UNKNOWN_OWNER
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "app-late")
        assert offload._current_offload_owner() == "app-late"
