"""Test doubles shared across the unit suite.

Import from here instead of copying a clock, a handler stub or a probe into the
next test file: ``from tests.unit._doubles import FakeClock``. A double that
only one module needs stays in that module; one that two modules need moves
here (FND-4942).

SQLAlchemy is imported inside :func:`record_connect_params`: it ships in an
optional extra, and nothing else here needs it.
"""

from __future__ import annotations

import asyncio
import functools
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from application_sdk._runtime.offload import run_in_thread
from application_sdk.handler import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    Handler,
    MetadataInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)

# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A monotonic clock the test advances explicitly.

    Inject it (``clock=``, a module-local ``_monotonic`` seam) rather than
    patching ``time.monotonic``: the asyncio loop shares that global, so
    patching it makes the loop itself misbehave. A clock with a different
    interface (one that advances on ``sleep``, one that reads ``datetime``)
    stays beside its test.
    """

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


class StaticHandler(Handler):
    """A handler whose three entry points succeed with nothing in them.

    For tests of the routes, the manifest or the host around a handler, not of
    the handler itself. Subclass it to override the one method a test is about.
    """

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        return SqlMetadataOutput(objects=[])


# ---------------------------------------------------------------------------
# Temporal interceptor inputs
# ---------------------------------------------------------------------------

#: When a :class:`WorkflowInfo` run started, in workflow (history) time.
WORKFLOW_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class ExecuteInput:
    """Stands in for Temporal's ``ExecuteWorkflowInput`` and ``ExecuteActivityInput``."""

    fn: Any = None
    args: list[Any] = field(default_factory=list)
    headers: dict[str, Any] = field(default_factory=dict)
    executor: Any = None


@dataclass
class ParentInfo:
    workflow_id: str = "parent-wf-id"
    run_id: str = "parent-run-id"


@dataclass
class WorkflowInfo:
    """Stands in for ``workflow.info()``."""

    start_time: datetime = WORKFLOW_START
    workflow_id: str = "wf-id"
    run_id: str = "run-id"
    workflow_type: str = "TestWorkflow"
    task_queue: str = "default"
    namespace: str = "ns"
    attempt: int = 1
    parent: ParentInfo | None = None


@dataclass
class ActivityInfo:
    """Stands in for ``activity.info()``.

    A module whose tests depend on other defaults subclasses it as a dataclass
    and overrides just those fields.
    """

    activity_id: str = "act-id"
    activity_type: str = "TestActivity"
    task_queue: str = "default"
    workflow_id: str = "wf-id"
    workflow_run_id: str = "run-id"
    workflow_type: str = "TestWorkflow"
    attempt: int = 1
    namespace: str = "ns"
    started_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    scheduled_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    start_to_close_timeout: timedelta | None = None
    schedule_to_close_timeout: timedelta | None = None
    heartbeat_timeout: timedelta | None = None


# ---------------------------------------------------------------------------
# SQLAlchemy connects
# ---------------------------------------------------------------------------


class StopConnect(Exception):
    """Raised by :func:`record_connect_params` in place of opening a socket."""


def record_connect_params(engine: Any) -> list[dict[str, Any]]:
    """Record each connect's driver keyword arguments, then stop it before any socket opens.

    *engine* is a sync ``Engine`` (an async engine's ``.sync_engine``). Register
    it after the listener under test, so it sees what the driver would get. Each
    connect then raises :class:`StopConnect`.
    """
    from sqlalchemy import event

    seen: list[dict[str, Any]] = []

    def _record(
        dialect: object, conn_rec: object, cargs: object, cparams: dict[str, Any]
    ) -> None:
        seen.append(dict(cparams))
        raise StopConnect

    event.listen(engine, "do_connect", _record)
    return seen


# ---------------------------------------------------------------------------
# Fresh-interpreter import probes
# ---------------------------------------------------------------------------


def loaded_after_running(
    code: str, *, without_env: tuple[str, ...] = ()
) -> frozenset[str]:
    """Run *code* in a fresh interpreter; return every name in ``sys.modules`` after.

    In-process, pytest and earlier tests have already imported most of the
    SDK, which hides an import that should not happen. ``.env`` loading is off
    (``application_sdk.constants`` would load the checkout's), and each name in
    *without_env* is cleared, so the host running the suite cannot decide what
    gets imported.
    """
    env = {k: v for k, v in os.environ.items() if k not in without_env}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return _probe(code, tuple(sorted(env.items())))


@functools.cache
def _probe(code: str, env: tuple[tuple[str, str], ...]) -> frozenset[str]:
    """The probe itself, cached on *code* and the child's whole environment.

    Each probe is an interpreter importing the SDK, 1-4s on Windows, and several
    tests ask for the same one. The environment is part of the key: a variable
    that changes what gets imported (an OTLP switch, say) must start a new probe,
    not return one taken before it changed.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import sys\n{code}\nprint('\\n'.join(sys.modules))"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=dict(env),
    )
    assert result.returncode == 0, (
        f"running {code!r} in a fresh interpreter failed:\n{result.stderr}"
    )
    return frozenset(line for line in result.stdout.splitlines() if line)


def loaded_after_importing(*modules: str) -> frozenset[str]:
    """Import *modules* in a fresh interpreter; return every name in ``sys.modules`` after."""
    imports = "".join(f"importlib.import_module({m!r});" for m in modules)
    return loaded_after_running(f"import importlib;{imports}")


# ---------------------------------------------------------------------------
# Fan-out first-failure probe
# ---------------------------------------------------------------------------

#: How long a sibling's offloaded call blocks its thread. Long enough that an
#: undrained fan-out raises well before it ends.
SIBLING_THREAD_SECONDS = 0.3

#: Bound on a sibling thread's wait for the failure to be raised. Generous:
#: a passing test never hits it, and it covers thread-pool spin-up.
FAILURE_WAIT_SECONDS = 30.0


class SiblingProbe:
    """Siblings that block in ``run_in_thread``, and one call that fails once
    every sibling's thread is running.

    ``completed`` records each sibling coroutine that ran past its thread. With
    ``asyncio.gather`` they did, after the caller had seen the failure; with the
    SDK fan-out they are cancelled, and their threads drained before the raise
    (FND-4277).
    """

    def __init__(self, siblings: int) -> None:
        self.started = [threading.Event() for _ in range(siblings)]
        self.finished = [threading.Event() for _ in range(siblings)]
        # Set as the failure is raised. A sibling thread holds its
        # SIBLING_THREAD_SECONDS until then, so a slow loop (the hops in
        # ``fail``) cannot let a sibling finish before the raise.
        self.failing = threading.Event()
        self.completed: list[int] = []

    async def sibling(self, index: int) -> None:
        def _block() -> None:
            self.started[index].set()
            self.failing.wait(FAILURE_WAIT_SECONDS)
            time.sleep(SIBLING_THREAD_SECONDS)
            self.finished[index].set()

        await run_in_thread(_block)
        self.completed.append(index)

    async def fail(self, exc: Exception) -> None:
        try:
            for event in self.started:
                if not await asyncio.to_thread(event.wait, 5.0):
                    raise AssertionError("sibling thread did not start")
        finally:
            # Release the sibling threads however ``fail`` exits.
            self.failing.set()
        raise exc

    async def assert_no_sibling_outlived_the_raise(self) -> None:
        assert all(e.is_set() for e in self.finished), (
            "a sibling's thread was still running when the failure propagated"
        )
        # Give an orphaned coroutine every chance to resume and finish.
        await asyncio.sleep(SIBLING_THREAD_SECONDS * 2)
        assert self.completed == [], "a sibling completed after the raise"
