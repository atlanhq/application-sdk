"""The preflight gate's warmup phase, through a real worker and a real server (FND-3039).

Everything below runs the generated workflow on the embedded Temporal dev server
against a worker built by ``create_worker``, so the activities under test are
the ones the worker really registers and the waits are real durable timers.
The handler is :class:`~application_sdk.testing.WarmingSourceHandler` around a
:class:`~application_sdk.testing.WarmingSource`: a scripted source whose warmup
walks whatever state sequence a test gives it (``COLD`` until started, one state
per call after), and which records every call the gate makes, so each test can
assert both the run's outcome and the exact calls that led to it.

Coverage is one test per way the wait can end: ``READY`` (after ``RUNNING`` and
after ``NOT_STARTED``), ``NOT_REQUIRED``, ``FAILED``, a typed AUTH /
PERMISSION / NOT_FOUND raise from either hook, a transient raise (polled
through), and the ceiling — plus soft mode, a failing ``WARMUP``-tier check,
and an app that declares no warmup.

The short-circuit tests pin *how many* ``warmup_state`` polls ran and that the
run ended well inside the ceiling. That is what makes them fail when the
short-circuit is removed: a gate that kept polling past ``FAILED`` would still
block eventually, at the ceiling, as ``SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from application_sdk.app.base import App
from application_sdk.app.context import AppContext
from application_sdk.contracts.base import Input, Output
from application_sdk.credentials.spec import AgentCredentialSpec
from application_sdk.errors.leaves import (
    AppPermissionDeniedError,
    AuthError,
    DependencyUnavailableError,
    NotFoundError,
)
from application_sdk.execution._temporal.preflight_gate import (
    PREFLIGHT_FAILED_ERROR_TYPE,
)
from application_sdk.execution.retry import NO_RETRY
from application_sdk.handler.contracts import WarmupState, WarmupStatus
from application_sdk.testing import (
    SourceState,
    WarmingSource,
    WarmingSourceHandler,
    WarmingStep,
)

pytestmark = pytest.mark.integration

CEILING_SECONDS = 5
POLL_SECONDS = 1

COLD = SourceState.COLD
WARMING = SourceState.WARMING
READY = SourceState.READY


def warming_fake(
    *script: WarmingStep, warmup_check_passes: bool = True
) -> WarmingSourceHandler:
    """A handler whose source starts cold and then walks ``script``."""
    return WarmingSourceHandler(
        WarmingSource([COLD, *script]), warmup_check_passes=warmup_check_passes
    )


# ---------------------------------------------------------------------------
# Apps under test (module level: the worker imports them via passthrough)
# ---------------------------------------------------------------------------


class WarmupInput(Input):
    # The three credential-routing fields make the input gate-eligible; left
    # empty, the gate resolves no credential and needs no secret store.
    extraction_method: str = ""
    credential_guid: str = ""
    agent_json: AgentCredentialSpec | None = None


class WarmupOutput(Output):
    ran: bool = False


class HardWarmupApp(App):
    preflight_gate_mode = "hard"
    preflight_gate_max_attempts = 1
    preflight_warmup_ceiling_seconds = CEILING_SECONDS
    preflight_warmup_poll_seconds = POLL_SECONDS

    async def run(self, input: WarmupInput) -> WarmupOutput:
        return WarmupOutput(ran=True)


class SoftWarmupApp(App):
    preflight_gate_mode = "soft"
    preflight_gate_max_attempts = 1
    preflight_warmup_ceiling_seconds = CEILING_SECONDS
    preflight_warmup_poll_seconds = POLL_SECONDS

    async def run(self, input: WarmupInput) -> WarmupOutput:
        return WarmupOutput(ran=True)


class NoWarmupApp(App):
    preflight_gate_mode = "hard"
    preflight_gate_max_attempts = 1

    async def run(self, input: WarmupInput) -> WarmupOutput:
        return WarmupOutput(ran=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, "cause", None) or exc.__cause__


def _block_primary(exc: BaseException) -> dict[str, Any]:
    """``details[0]`` of the ``PreflightFailed`` block in ``exc``'s chain."""
    for link in _chain(exc):
        if getattr(link, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE:
            details = list(getattr(link, "details", None) or [])
            assert details, "the block carries no FailureDetails"
            primary = details[0]
            return primary if isinstance(primary, dict) else primary.model_dump()
    raise AssertionError(f"no {PREFLIGHT_FAILED_ERROR_TYPE} block in {exc!r}")


async def _run(run_worker, executor, reregister_app, app_cls, fake, timing=None):
    """Run ``app_cls`` once against ``fake``.

    ``timing``, when given, receives the seconds the workflow itself took —
    worker start and graceful shutdown excluded, since they dwarf a short wait.
    """
    reregister_app(app_cls)
    async with run_worker(handler=fake, enable_sdr=False):
        began = time.monotonic()
        try:
            return await executor.execute(
                app_cls,
                WarmupInput(),
                context=AppContext(app_name=app_cls._app_name, app_version="1.0.0"),
                retry_policy=NO_RETRY,
            )
        finally:
            if timing is not None:
                timing.append(time.monotonic() - began)


async def _blocked(run_worker, executor, reregister_app, app_cls, fake):
    """Run expecting the gate's block; returns ``(details[0], seconds taken)``."""
    timing: list[float] = []
    with pytest.raises(Exception) as caught:
        await _run(run_worker, executor, reregister_app, app_cls, fake, timing)
    return _block_primary(caught.value), timing[0]


def _ran(result: Any) -> bool:
    return result["ran"] if isinstance(result, dict) else result.ran


# ---------------------------------------------------------------------------
# The wait ends in READY: the WARMUP checks run, then the app
# ---------------------------------------------------------------------------


class TestReadyRunsTheWarmupChecks:
    async def test_running_then_ready(self, run_worker, executor, reregister_app):
        fake = warming_fake(WARMING, WARMING, READY)
        result = await _run(run_worker, executor, reregister_app, HardWarmupApp, fake)
        assert _ran(result) is True
        assert fake.check_tiers == ["fast", "warmup"]
        assert fake.calls.count("start") == 1
        assert fake.polls == 2
        # The WARMUP checks wait on READY: nothing runs after them.
        assert fake.calls[-1] == "check:warmup"

    async def test_not_started_is_polled_like_running(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(COLD, COLD, READY)
        result = await _run(run_worker, executor, reregister_app, HardWarmupApp, fake)
        assert _ran(result) is True
        assert fake.polls == 2
        assert fake.check_tiers == ["fast", "warmup"]

    async def test_ready_at_start_needs_no_poll(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(READY)
        result = await _run(run_worker, executor, reregister_app, HardWarmupApp, fake)
        assert _ran(result) is True
        assert fake.polls == 0
        assert fake.check_tiers == ["fast", "warmup"]

    async def test_not_required_runs_the_warmup_checks_at_once(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(WarmupState(status=WarmupStatus.NOT_REQUIRED))
        result = await _run(run_worker, executor, reregister_app, HardWarmupApp, fake)
        assert _ran(result) is True
        assert fake.polls == 0
        assert fake.check_tiers == ["fast", "warmup"]

    async def test_a_transient_raise_is_polled_through(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(
            WARMING,
            DependencyUnavailableError(message="resume API returned 503"),
            RuntimeError("socket reset"),
            READY,
        )
        result = await _run(run_worker, executor, reregister_app, HardWarmupApp, fake)
        assert _ran(result) is True
        assert fake.polls == 3

    async def test_a_failing_warmup_check_blocks_on_that_check(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(WARMING, READY, warmup_check_passes=False)
        primary, _ = await _blocked(
            run_worker, executor, reregister_app, HardWarmupApp, fake
        )
        assert primary["message"] == "catalog scan found no schemas"
        assert fake.check_tiers == ["fast", "warmup"]


# ---------------------------------------------------------------------------
# The wait short-circuits: FAILED, or a typed terminal raise
# ---------------------------------------------------------------------------


class TestTerminalStatesFailImmediately:
    @pytest.mark.parametrize(
        ("failed", "message"),
        [
            (
                WarmupState(
                    status=WarmupStatus.FAILED, message="warehouse is decommissioned"
                ),
                "warehouse is decommissioned",
            ),
            (SourceState.UNAVAILABLE, "Source is unavailable and did not warm up."),
        ],
        ids=["untyped_failed", "unavailable"],
    )
    async def test_failed_blocks_on_the_first_poll(
        self, run_worker, executor, reregister_app, failed, message
    ):
        fake = warming_fake(WARMING, failed)
        primary, took = await _blocked(
            run_worker, executor, reregister_app, HardWarmupApp, fake
        )
        assert primary["code"] == "SOURCE_UNAVAILABLE"
        assert primary["audience"] == "USER"
        assert primary["message"] == message
        assert fake.polls == 1
        assert took < CEILING_SECONDS
        assert fake.check_tiers == ["fast"]

    @pytest.mark.parametrize(
        ("raised", "code"),
        [
            (AuthError(message="token expired"), "AUTH"),
            (AppPermissionDeniedError(message="no USAGE on warehouse"), "PERMISSION"),
            (NotFoundError(message="warehouse not found"), "NOT_FOUND"),
        ],
        ids=["auth", "permission", "not_found"],
    )
    async def test_a_typed_raise_from_warmup_state_blocks_at_once(
        self, run_worker, executor, reregister_app, raised, code
    ):
        fake = warming_fake(WARMING, raised)
        primary, took = await _blocked(
            run_worker, executor, reregister_app, HardWarmupApp, fake
        )
        assert primary["code"] == code
        assert fake.polls == 1
        assert took < CEILING_SECONDS
        assert fake.check_tiers == ["fast"]

    async def test_a_typed_raise_from_warmup_start_blocks_without_polling(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(AuthError(message="token expired"))
        primary, took = await _blocked(
            run_worker, executor, reregister_app, HardWarmupApp, fake
        )
        assert primary["code"] == "AUTH"
        assert fake.polls == 0
        assert took < CEILING_SECONDS


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


class TestTheCeiling:
    async def test_still_warming_at_the_ceiling_is_warmup_exhausted(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(WARMING)
        primary, took = await _blocked(
            run_worker, executor, reregister_app, HardWarmupApp, fake
        )
        assert primary["code"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert primary["category"] == "SOURCE_UNAVAILABLE"
        assert primary["audience"] == "USER"
        assert f"wasn't ready within {CEILING_SECONDS}s" in primary["message"]
        # The hook's message, not its status: the source's own word for it.
        assert "last reported: WARMING" in primary["message"]
        assert took >= CEILING_SECONDS
        # Polled on the timer, not in a loop: about one poll per interval.
        assert 2 <= fake.polls <= CEILING_SECONDS // POLL_SECONDS + 1
        assert fake.check_tiers == ["fast"]

    async def test_the_ceiling_names_the_last_transient_error(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(
            WARMING, DependencyUnavailableError(message="resume API returned 503")
        )
        primary, _ = await _blocked(
            run_worker, executor, reregister_app, HardWarmupApp, fake
        )
        assert primary["code"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert "resume API returned 503" in primary["message"]


# ---------------------------------------------------------------------------
# Posture, and apps without a warmup
# ---------------------------------------------------------------------------


class TestPostureAndOptIn:
    async def test_soft_mode_reports_a_failed_warmup_and_proceeds(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(WARMING, AuthError(message="token expired"))
        result = await _run(run_worker, executor, reregister_app, SoftWarmupApp, fake)
        assert _ran(result) is True
        assert fake.polls == 1
        assert fake.check_tiers == ["fast"]

    async def test_an_app_without_a_warmup_never_calls_the_hooks(
        self, run_worker, executor, reregister_app
    ):
        fake = warming_fake(WARMING)
        result = await _run(run_worker, executor, reregister_app, NoWarmupApp, fake)
        assert _ran(result) is True
        assert fake.calls == ["check:all"]
