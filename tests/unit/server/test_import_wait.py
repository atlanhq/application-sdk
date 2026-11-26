"""Tests for the import-wait liveness check (FND-5531).

The in-process tests block real threads on a real module import lock, so they
read the same ``importlib`` state the check reads in production. The
subprocess test reproduces the wedge that Temporal's deadlock interrupt causes
and shows ``check_live()`` failing on it, and staying healthy without it.
"""

import json
import subprocess
import sys
import threading
import time
import traceback
import types
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from application_sdk.server import _import_wait
from application_sdk.server._import_wait import (
    ImportWaitWatch,
    _import_site,
    _module_lock_waits,
)
from application_sdk.server.health import WorkerHealthServer


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _BlockedImport:
    """One thread holds a module's import lock; another waits on it."""

    module: str
    waiter: threading.Thread
    release: threading.Event


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def blocked_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_BlockedImport]:
    name = f"_import_wait_target_{uuid.uuid4().hex}"
    gate_name = f"{name}_gate"
    gate = types.ModuleType(gate_name)
    gate.release = threading.Event()
    gate.entered = threading.Event()
    monkeypatch.setitem(sys.modules, gate_name, gate)
    (tmp_path / f"{name}.py").write_text(
        f"import {gate_name} as g\ng.entered.set()\ng.release.wait(10)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    def do_import() -> None:
        __import__(name)

    holder = threading.Thread(target=do_import, daemon=True)
    holder.start()
    assert gate.entered.wait(5)
    waiter = threading.Thread(target=do_import, name="waiter", daemon=True)
    waiter.start()
    assert _wait_until(
        lambda: any(w.thread_ident == waiter.ident for w in _module_lock_waits() or [])
    )
    try:
        yield _BlockedImport(module=name, waiter=waiter, release=gate.release)
    finally:
        gate.release.set()
        holder.join(5)
        waiter.join(5)
        sys.modules.pop(name, None)


class TestModuleLockWaits:
    def test_reads_this_interpreters_blocking_on_table(self):
        """Pins the private ``_blocking_on`` shape on every Python we test.

        If an interpreter upgrade changes it, the check fails open in
        production; this test makes that upgrade fail CI instead.
        """
        assert _module_lock_waits() is not None

    def test_sees_a_thread_waiting_on_an_import(self, blocked_import):
        waits = _module_lock_waits()

        assert waits is not None
        matching = [w for w in waits if w.thread_ident == blocked_import.waiter.ident]
        assert [w.module for w in matching] == [blocked_import.module]
        assert matching[0].thread_name == "waiter"

    def test_unrecognised_table_is_reported_as_unsupported(self):
        with patch.object(_import_wait._bootstrap, "_blocking_on", None):
            assert _module_lock_waits() is None


def _frame(filename: str, name: str) -> traceback.FrameSummary:
    return traceback.FrameSummary(filename, 10, name, lookup_line=False)


class TestImportSite:
    def test_skips_temporal_sandbox_importer_to_reach_workflow_code(self):
        stack = traceback.StackSummary.from_list(
            [
                _frame("/app/worker.py", "activate"),
                _frame("/app/workflows/extract.py", "run"),
                _frame(
                    "/venv/temporalio/worker/workflow_sandbox/_importer.py", "_import"
                ),
                _frame("<frozen importlib._bootstrap>", "_find_and_load"),
                _frame("/app/heavy/__init__.py", "<module>"),
                _frame("<frozen importlib._bootstrap>", "_find_and_load"),
            ]
        )

        assert _import_site(stack) == "/app/workflows/extract.py:10 in run"

    def test_stack_without_import_machinery_has_no_site(self):
        stack = traceback.StackSummary.from_list([_frame("/app/a.py", "f")])

        assert _import_site(stack) is None


class TestImportWaitWatch:
    def test_no_waits_is_healthy(self):
        clock = _Clock()
        watch = ImportWaitWatch(10, clock=clock)
        assert watch.check() is None
        clock.now += 100
        assert watch.check() is None

    def test_wait_within_bound_is_not_reported(self, blocked_import):
        clock = _Clock()
        watch = ImportWaitWatch(10, clock=clock)
        assert watch.check() is None
        clock.now += 10
        # Equality is within the bound: the comparison is a strict ``>``.
        assert watch.check() is None

    def test_wait_past_bound_is_reported(self, blocked_import):
        clock = _Clock()
        watch = ImportWaitWatch(10, clock=clock)
        watch.check()
        clock.now += 11

        stuck = watch.check()

        assert stuck is not None
        assert stuck.module == blocked_import.module
        assert stuck.thread == "waiter"
        assert stuck.waited_seconds == 11
        # The import was started by ``do_import`` in this file's fixture.
        assert stuck.import_site is not None
        assert stuck.import_site.startswith(f"{__file__}:")
        assert stuck.import_site.endswith(" in do_import")

    def test_first_sighting_starts_the_clock(self, blocked_import):
        """A wait is timed from the first check that sees it, not from an
        earlier check that saw nothing."""
        clock = _Clock()
        watch = ImportWaitWatch(10, clock=clock)
        clock.now += 100
        assert watch.check() is None

    def test_wait_that_ends_is_forgotten(self, blocked_import):
        clock = _Clock()
        watch = ImportWaitWatch(10, clock=clock)
        watch.check()
        blocked_import.release.set()
        blocked_import.waiter.join(5)
        watch.check()
        clock.now += 100

        assert watch.check() is None

    def test_stuck_wait_is_logged_once(self, blocked_import):
        clock = _Clock()
        watch = ImportWaitWatch(10, clock=clock)
        watch.check()
        clock.now += 11
        with patch.object(_import_wait, "logger") as logger:
            watch.check()
            clock.now += 10
            watch.check()

        assert logger.error.call_count == 1
        # The log names the import site and carries the waiting thread's stack,
        # so the culprit line can be found from the log alone.
        args = logger.error.call_args.args
        rendered = args[0] % args[1:]
        assert f"started at {__file__}:" in rendered
        assert "in do_import" in rendered
        assert "Stack of the waiting thread:" in rendered

    def test_unsupported_interpreter_fails_open_and_warns_once(self):
        watch = ImportWaitWatch(10, clock=_Clock())
        with (
            patch.object(_import_wait._bootstrap, "_blocking_on", None),
            patch.object(_import_wait, "logger") as logger,
        ):
            assert watch.check() is None
            assert watch.check() is None

        assert logger.warning.call_count == 1


class TestCheckLiveImportWait:
    async def test_stuck_import_fails_live(self, blocked_import):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_import_wait_seconds=10
        )
        clock = _Clock()
        server._import_wait_watch = ImportWaitWatch(10, clock=clock)
        assert (await server.check_live()).healthy is True
        clock.now += 11

        status = await server.check_live()

        assert status.healthy is False
        assert status.message == "Worker import machinery is wedged"
        assert status.details["stuck_import_module"] == blocked_import.module
        assert status.details["stuck_import_thread"] == "waiter"
        assert status.details["stuck_import_waited_seconds"] == 11
        assert status.details["max_import_wait_seconds"] == 10
        assert status.details["stuck_import_site"].endswith(" in do_import")
        assert "poller_counts" in status.details

    @pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan")])
    def test_non_positive_or_non_finite_bound_disables_check(self, bad: float):
        server = WorkerHealthServer(
            host="127.0.0.1", port=0, max_import_wait_seconds=bad
        )
        assert server._import_wait_watch is None

    def test_disabled_by_default_on_the_constructor(self):
        assert WorkerHealthServer(host="127.0.0.1", port=0)._import_wait_watch is None

    def test_builder_wires_the_bound_from_constants(self):
        from application_sdk.server.health import build_worker_health_server

        @dataclass(frozen=True)
        class _Client:
            identity: str

        with patch(
            "application_sdk.constants.WORKER_LIVENESS_MAX_IMPORT_WAIT_SECONDS", 42.0
        ):
            server = build_worker_health_server(port=0, client=_Client("1@host"))

        assert server._max_import_wait_seconds == 42.0
        assert server._import_wait_watch is not None


_SCRIPT = Path(__file__).with_name("_wedge_import_script.py")


def _run_wedge_script(mode: str, workdir: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(workdir), mode],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    # The SDK logger also writes to stdout; the result is the last line.
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestDeadlockInterruptWedge:
    """Red-green proof against a real interpreter (FND-5531)."""

    def test_interrupt_mid_import_fails_live(self, tmp_path: Path):
        result = _run_wedge_script("interrupt", tmp_path)

        # The wedge itself: the next waiter is stranded holding p's lock.
        assert result["wakeup_lock_leaked"] is True
        assert result["c_stranded"] is True
        assert result["import_p_completes"] is False
        # Why the check does not try a fresh import: that still works.
        assert result["fresh_module_import_completes"] is True
        # First sighting starts the clock; past the bound /live fails.
        assert result["first_live"]["healthy"] is True
        assert result["second_live"]["healthy"] is False
        details = result["second_live"]["details"]
        assert details["stuck_import_module"] == "m"
        # The import chain was started by the script's ``run`` (the stand-in
        # for workflow code), not by ``p/__init__.py`` deeper in the chain.
        assert "_wedge_import_script.py:" in details["stuck_import_site"]
        assert details["stuck_import_site"].endswith(" in run")

    def test_same_sequence_without_interrupt_stays_live(self, tmp_path: Path):
        result = _run_wedge_script("control", tmp_path)

        assert result["wakeup_lock_leaked"] is False
        assert result["c_stranded"] is False
        assert result["import_p_completes"] is True
        assert result["first_live"]["healthy"] is True
        assert result["second_live"]["healthy"] is True
