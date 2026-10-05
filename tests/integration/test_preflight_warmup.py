"""The preflight gate's warmup wait, through a real worker on a time-skipping server.

Everything below runs the generated workflow against a worker built by
``create_worker``, so the activities under test are the ones the worker really
registers — the check activity and ``{app}:preflight_warmup`` — and the waits
are real durable timers. The server is Temporal's time-skipping test server
(this module overrides the suite's ``temporal_client``), so a 30s ceiling costs
no wall-clock time; durations are asserted in workflow time, off the rows'
``warmup_duration_ms`` and the history's timers.

The handler is :class:`~application_sdk.testing.WarmingSourceHandler` around a
:class:`~application_sdk.testing.WarmingSource`: a scripted source whose probes
walk the states a test gives it, and which records every call the gate makes
(``"probe"``, ``"check:<tiers>"``), so each test asserts both the run's outcome
and the exact calls that led to it.

Coverage is one test per way the gate can go: ``READY`` on the first probe (one
check dispatch with every tier, no timer, no poll); a cold source warmed to
``READY`` (``PREFLIGHT`` first, then polls on timers, then ``WARMUP``); the
ceiling under each warmup posture; ``UNAVAILABLE`` from the first probe and from
a poll; a typed AUTH raise from the first probe and from a poll under each gate
posture. Rows are read from both frames: the activity's through
:func:`~application_sdk.testing.capture_preflight_outcomes`, the workflow's by
wrapping ``application_sdk.app.base._safe_log`` (the module is passed through
the sandbox, so the worker's workflow calls the wrapper).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

import application_sdk.app.base as app_base
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
    preflight_gate_activity_name,
    preflight_warmup_activity_name,
)
from application_sdk.execution._temporal.workflows import get_all_app_workflows
from application_sdk.execution.retry import NO_RETRY
from application_sdk.execution.sandbox import SandboxConfig
from application_sdk.handler.contracts import (
    PreflightGateMode,
    WarmupObservation,
    WarmupState,
)
from application_sdk.observability.logger_adaptor import (
    GATE_MODE_KEY,
    GATE_TIER_KEY,
    WARMUP_DURATION_KEY,
    WARMUP_OUTCOME_KEY,
    WARMUP_TRANSITIONS_KEY,
)
from application_sdk.testing import (
    OUTCOME_LEVELS,
    PreflightOutcomeCapture,
    WarmingSource,
    WarmingSourceHandler,
    WarmingStep,
)
from application_sdk.testing.preflight import (  # noqa: F401 — fixture, requested by gate_rows
    capture_preflight_outcomes,
)

pytestmark = pytest.mark.integration

CEILING_SECONDS = 30
"""The ceiling's floor: the smallest wait an app can declare."""

COLD = WarmupState.COLD
WARMING = WarmupState.WARMING
READY = WarmupState.READY
UNAVAILABLE = WarmupState.UNAVAILABLE


def warming_fake(
    *script: WarmingStep, warmup_check_passes: bool = True
) -> WarmingSourceHandler:
    """A handler whose source answers ``script``, one step per probe."""
    return WarmingSourceHandler(
        WarmingSource(list(script)), warmup_check_passes=warmup_check_passes
    )


# ---------------------------------------------------------------------------
# Apps under test (module level: the worker imports them via passthrough)
# ---------------------------------------------------------------------------


class WarmupAppInput(Input):
    # The three credential-routing fields make the input gate-eligible; left
    # empty, the gate resolves no credential and needs no secret store.
    extraction_method: str = ""
    credential_guid: str = ""
    agent_json: AgentCredentialSpec | None = None


class WarmupAppOutput(Output):
    ran: bool = False


class HardGateApp(App):
    """Hard gate, default (soft) warmup posture."""

    preflight_gate_mode = PreflightGateMode.HARD
    preflight_gate_max_attempts = 1
    preflight_warmup_ceiling_seconds = CEILING_SECONDS

    async def run(self, input: WarmupAppInput) -> WarmupAppOutput:
        return WarmupAppOutput(ran=True)


class SoftGateApp(App):
    """Soft gate, default (soft) warmup posture."""

    preflight_gate_mode = PreflightGateMode.SOFT
    preflight_gate_max_attempts = 1
    preflight_warmup_ceiling_seconds = CEILING_SECONDS

    async def run(self, input: WarmupAppInput) -> WarmupAppOutput:
        return WarmupAppOutput(ran=True)


class HardWarmupApp(App):
    """Hard gate *and* hard warmup posture: a source that never warms stops it."""

    preflight_gate_mode = PreflightGateMode.HARD
    preflight_gate_max_attempts = 1
    preflight_warmup_ceiling_seconds = CEILING_SECONDS
    preflight_warmup_mode = PreflightGateMode.HARD

    async def run(self, input: WarmupAppInput) -> WarmupAppOutput:
        return WarmupAppOutput(ran=True)


# ---------------------------------------------------------------------------
# Server, row capture
# ---------------------------------------------------------------------------


@pytest.fixture
async def temporal_client() -> AsyncIterator[Client]:
    """A time-skipping test server, in place of the suite's embedded dev server.

    Overrides ``conftest.temporal_client``, so the suite's ``run_worker`` and
    ``executor`` fixtures run against it unchanged.
    """
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as env:
        yield env.client


@pytest.fixture
def gate_rows(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[PreflightOutcomeCapture]:
    """Every gate outcome row the run emits, from both frames, in order.

    The activity's rows reach the capture through the gate logger it patches;
    the workflow's go through ``_safe_log``, wrapped here to hand outcome rows
    to the same capture (a replayed workflow task re-runs the frame, so replay
    is skipped, as ``workflow.logger`` itself skips it).
    """
    capture: PreflightOutcomeCapture = request.getfixturevalue(
        "capture_preflight_outcomes"
    )
    real = app_base._safe_log

    def _safe_log(level: str, message: str, **attrs: Any) -> None:
        if level in OUTCOME_LEVELS and not workflow.unsafe.is_replaying():
            getattr(capture, level)(message, **attrs)
        real(level, message, **attrs)

    monkeypatch.setattr(app_base, "_safe_log", _safe_log)
    yield capture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Run:
    """One finished run: its result (``None`` when it raised) and its history."""

    handle: WorkflowHandle[Any, Any]
    result: WarmupAppOutput | None
    error: BaseException | None

    @property
    def ran(self) -> bool:
        return self.result is not None and self.result.ran


async def _run(
    run_worker: Any,
    executor: Any,
    reregister_app: Any,
    temporal_client: Client,
    app_cls: type[App],
    fake: WarmingSourceHandler,
) -> Run:
    """Run ``app_cls`` once against ``fake``; capture its result or its failure.

    Through ``executor.execute`` — the time-skipping client skips time only
    while a handle it started is awaited — with the workflow ID supplied, so
    the run's history can be fetched afterwards.
    """
    reregister_app(app_cls)
    workflow_id = f"warmup-{uuid4().hex[:12]}"
    result: WarmupAppOutput | None = None
    error: BaseException | None = None
    async with run_worker(handler=fake, enable_sdr=False):
        try:
            raw = await executor.execute(
                app_cls,
                WarmupAppInput(workflow_id=workflow_id),
                context=AppContext(app_name=app_cls._app_name, app_version="1.0.0"),
                retry_policy=NO_RETRY,
            )
        except Exception as e:
            error = e
        else:
            result = (
                raw
                if isinstance(raw, WarmupAppOutput)
                else WarmupAppOutput.model_validate(raw)
            )
    return Run(
        handle=temporal_client.get_workflow_handle(workflow_id),
        result=result,
        error=error,
    )


def _chain(exc: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = getattr(exc, "cause", None) or exc.__cause__


def _block_primary(run: Run) -> dict[str, Any]:
    """``details[0]`` of the ``PreflightFailed`` block that ended ``run``."""
    assert run.error is not None, f"the run was not blocked: {run.result!r}"
    for link in _chain(run.error):
        if getattr(link, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE:
            details = list(getattr(link, "details", None) or [])
            assert details, "the block carries no FailureDetails"
            primary = details[0]
            return primary if isinstance(primary, dict) else primary.model_dump()
    raise AssertionError(f"no {PREFLIGHT_FAILED_ERROR_TYPE} block in {run.error!r}")


async def _activities(run: Run) -> list[str]:
    """The activity types the run scheduled, in order."""
    history = await run.handle.fetch_history()
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


async def _timers(run: Run) -> list[float]:
    """Each durable timer the run started, in seconds, in order."""
    history = await run.handle.fetch_history()
    return [
        e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta().total_seconds()
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_TIMER_STARTED
    ]


def _transitions(row: dict[str, Any]) -> list[str]:
    """The states a row's ``warmup_transitions`` recorded, in order."""
    return [t["state"] for t in json.loads(row[WARMUP_TRANSITIONS_KEY])]


def _gate(app_cls: type[App]) -> str:
    return preflight_gate_activity_name(app_cls._app_name)


def _poll(app_cls: type[App]) -> str:
    return preflight_warmup_activity_name(app_cls._app_name)


# ---------------------------------------------------------------------------
# READY on the first probe: the gate is what it was before warmup existed
# ---------------------------------------------------------------------------


class TestReadyAtTheFirstProbe:
    async def test_one_dispatch_runs_every_tier_and_nothing_waits(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        fake = warming_fake(READY)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert run.ran is True
        assert fake.calls == ["probe", "check:preflight+warmup"]
        assert await _activities(run) == [_gate(HardGateApp)]
        assert await _timers(run) == []
        row = gate_rows.one
        assert row["outcome"] == "proceeded"
        # Identical to a pre-warmup row: no tier, no warmup fields.
        assert GATE_TIER_KEY not in row
        assert WARMUP_OUTCOME_KEY not in row

    async def test_its_history_replays_with_no_new_commands(
        self, run_worker, executor, reregister_app, temporal_client
    ):
        """A READY run's history has a pre-warmup run's shape — one check
        activity, no timer — and replays against this build unchanged."""
        run = await _run(
            run_worker,
            executor,
            reregister_app,
            temporal_client,
            HardGateApp,
            warming_fake(READY),
        )
        assert run.ran is True
        await _replay(run)


async def _replay(run: Run) -> None:
    """Replay ``run``'s history on this build; raises on any non-determinism."""
    replayer = Replayer(
        workflows=get_all_app_workflows(),
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxConfig()
            .with_passthrough_modules("tests")
            .to_temporal_restrictions()
        ),
        data_converter=pydantic_data_converter,
    )
    replayed = await replayer.replay_workflow(
        await run.handle.fetch_history(), raise_on_replay_failure=False
    )
    assert replayed.replay_failure is None, replayed.replay_failure


# ---------------------------------------------------------------------------
# Cold, then ready: PREFLIGHT, polls on timers, WARMUP, then the app
# ---------------------------------------------------------------------------


class TestColdThenReady:
    async def test_preflight_then_polls_then_warmup_then_extraction(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        fake = warming_fake(COLD, WARMING, READY)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert run.ran is True
        assert fake.calls == [
            "probe",
            "check:preflight",
            "probe",
            "probe",
            "check:warmup",
        ]
        gate, poll = _gate(HardGateApp), _poll(HardGateApp)
        assert await _activities(run) == [gate, poll, poll, gate]
        # 5s doubling: the wait is on durable timers, not in an activity.
        assert await _timers(run) == [5.0, 10.0]

        first, second = gate_rows.rows
        assert first["outcome"] == "proceeded"
        assert first[GATE_TIER_KEY] == "preflight"
        assert first[WARMUP_OUTCOME_KEY] == "warming"
        assert WARMUP_DURATION_KEY not in first
        assert second["outcome"] == "proceeded"
        assert second[GATE_TIER_KEY] == "warmup"
        assert second[WARMUP_OUTCOME_KEY] == "ready"
        assert second[WARMUP_DURATION_KEY] >= 15_000
        assert _transitions(second) == ["cold", "warming", "ready"]

    async def test_the_waited_history_replays_deterministically(
        self, run_worker, executor, reregister_app, temporal_client
    ):
        run = await _run(
            run_worker,
            executor,
            reregister_app,
            temporal_client,
            HardGateApp,
            warming_fake(COLD, WARMING, READY),
        )
        assert run.ran is True
        await _replay(run)

    async def test_a_poll_hint_sets_the_next_wait(
        self, run_worker, executor, reregister_app, temporal_client
    ):
        fake = warming_fake(
            WarmupObservation(state=WarmupState.QUEUED, next_poll_seconds=12), READY
        )
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert run.ran is True
        assert await _timers(run) == [12.0]

    async def test_a_transient_raise_is_polled_through(
        self, run_worker, executor, reregister_app, temporal_client
    ):
        # The first probe's 5s hint keeps the backoff short, so READY lands
        # inside the ceiling: 5s, 5s, 10s.
        fake = warming_fake(
            WarmupObservation(state=COLD, next_poll_seconds=5),
            DependencyUnavailableError(message="resume API returned 503"),
            RuntimeError("socket reset"),
            READY,
        )
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert run.ran is True
        assert fake.probes == 4
        assert await _timers(run) == [5.0, 5.0, 10.0]
        assert fake.check_tiers == ["preflight", "warmup"]

    async def test_a_failing_warmup_check_blocks_on_that_check(
        self, run_worker, executor, reregister_app, temporal_client
    ):
        fake = warming_fake(COLD, READY, warmup_check_passes=False)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert run.ran is False
        assert _block_primary(run)["message"] == "catalog scan found no schemas"
        assert fake.check_tiers == ["preflight", "warmup"]


# ---------------------------------------------------------------------------
# The ceiling: the warmup posture alone decides
# ---------------------------------------------------------------------------


class TestTheCeiling:
    async def test_hard_warmup_posture_blocks_as_warmup_exhausted(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        fake = warming_fake(COLD, WARMING)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardWarmupApp, fake
        )
        assert run.ran is False
        primary = _block_primary(run)
        assert primary["code"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert primary["category"] == "SOURCE_UNAVAILABLE"
        assert primary["audience"] == "USER"
        assert f"wasn't ready within {CEILING_SECONDS}s" in primary["message"]
        # The WARMUP checks never ran.
        assert fake.check_tiers == ["preflight"]
        # Polled on timers up to the ceiling, the last wait cut to fit it.
        timers = await _timers(run)
        assert timers[:2] == [5.0, 10.0]
        assert sum(timers) == pytest.approx(CEILING_SECONDS, abs=0.5)
        assert fake.probes == 1 + len(timers)

        exhausted = gate_rows.rows[-1]
        assert exhausted["outcome"] == "warmup_exhausted"
        assert exhausted["reason"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert exhausted[GATE_MODE_KEY] == "hard"
        assert exhausted[GATE_TIER_KEY] == "warmup"
        assert exhausted[WARMUP_OUTCOME_KEY] == "exhausted"
        assert exhausted[WARMUP_DURATION_KEY] >= CEILING_SECONDS * 1000
        assert _transitions(exhausted) == ["cold", "warming"]

    async def test_soft_warmup_posture_reports_and_proceeds_under_a_hard_gate(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        fake = warming_fake(COLD, WARMING)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert run.ran is True
        assert fake.check_tiers == ["preflight"]
        exhausted = gate_rows.rows[-1]
        assert exhausted["outcome"] == "warmup_exhausted"
        # Stamped with the posture that decided it, not the gate's.
        assert exhausted[GATE_MODE_KEY] == "soft"

    async def test_a_ready_that_lands_at_the_ceiling_is_exhausted(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        """The last wait is cut to end at the ceiling; READY from that poll
        arrived late and does not buy the WARMUP checks extra time."""
        fake = warming_fake(COLD, WARMING, WARMING, READY)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardWarmupApp, fake
        )
        assert run.ran is False
        assert _block_primary(run)["code"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert fake.probes == 4
        assert fake.check_tiers == ["preflight"]
        exhausted = gate_rows.rows[-1]
        assert exhausted["outcome"] == "warmup_exhausted"
        assert _transitions(exhausted) == ["cold", "warming", "ready"]

    async def test_the_ceiling_names_the_last_transient_error(
        self, run_worker, executor, reregister_app, temporal_client
    ):
        fake = warming_fake(
            COLD, DependencyUnavailableError(message="resume API returned 503")
        )
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardWarmupApp, fake
        )
        primary = _block_primary(run)
        assert primary["code"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert "resume API returned 503" in primary["message"]


# ---------------------------------------------------------------------------
# UNAVAILABLE ends the wait
# ---------------------------------------------------------------------------


class TestUnavailable:
    async def test_from_a_poll_it_ends_the_wait_at_once(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        fake = warming_fake(COLD, UNAVAILABLE, READY)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardWarmupApp, fake
        )
        assert run.ran is False
        assert fake.probes == 2
        assert await _timers(run) == [5.0]
        assert fake.check_tiers == ["preflight"]
        assert _block_primary(run)["category"] == "SOURCE_UNAVAILABLE"
        row = gate_rows.rows[-1]
        assert row["outcome"] == "blocked"
        assert row[WARMUP_OUTCOME_KEY] == "unavailable"
        assert row[GATE_MODE_KEY] == "hard"

    async def test_from_the_first_probe_nothing_is_polled(
        self, run_worker, executor, reregister_app, temporal_client, gate_rows
    ):
        fake = warming_fake(UNAVAILABLE, READY)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        # Soft warmup posture: reported, and the run goes on.
        assert run.ran is True
        assert fake.calls == ["probe", "check:preflight"]
        assert await _activities(run) == [_gate(HardGateApp)]
        first, last = gate_rows.rows
        assert first[WARMUP_OUTCOME_KEY] == "unavailable"
        assert last["outcome"] == "would_block"
        assert last[WARMUP_OUTCOME_KEY] == "unavailable"
        assert last[GATE_MODE_KEY] == "soft"


# ---------------------------------------------------------------------------
# A typed raise from the probe is a verdict: the gate posture decides
# ---------------------------------------------------------------------------


class TestTypedRaise:
    @pytest.mark.parametrize(
        ("app_cls", "blocks"),
        [(HardGateApp, True), (SoftGateApp, False)],
        ids=["hard_gate", "soft_gate"],
    )
    async def test_auth_from_a_poll(
        self,
        run_worker,
        executor,
        reregister_app,
        temporal_client,
        gate_rows,
        app_cls,
        blocks,
    ):
        fake = warming_fake(COLD, AuthError(message="token expired"))
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, app_cls, fake
        )
        assert run.ran is not blocks
        assert fake.probes == 2
        assert await _timers(run) == [5.0]
        assert fake.check_tiers == ["preflight"]
        row = gate_rows.rows[-1]
        assert row["outcome"] == ("blocked" if blocks else "would_block")
        assert row["reason"] == "AUTH"
        assert row[WARMUP_OUTCOME_KEY] == "failed"
        if blocks:
            assert _block_primary(run)["code"] == "AUTH"

    @pytest.mark.parametrize(
        ("app_cls", "blocks"),
        [(HardGateApp, True), (SoftGateApp, False)],
        ids=["hard_gate", "soft_gate"],
    )
    async def test_auth_from_the_first_probe(
        self,
        run_worker,
        executor,
        reregister_app,
        temporal_client,
        gate_rows,
        app_cls,
        blocks,
    ):
        fake = warming_fake(AuthError(message="token expired"), READY)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, app_cls, fake
        )
        assert run.ran is not blocks
        # No checks, no wait: the probe's raise is the verdict.
        assert fake.calls == ["probe"]
        assert await _activities(run) == [_gate(app_cls)]
        row = gate_rows.one
        assert row["outcome"] == ("blocked" if blocks else "would_block")
        assert row["reason"] == "AUTH"
        if blocks:
            assert _block_primary(run)["code"] == "AUTH"

    @pytest.mark.parametrize(
        ("raised", "code"),
        [
            (AppPermissionDeniedError(message="no USAGE on warehouse"), "PERMISSION"),
            (NotFoundError(message="warehouse not found"), "NOT_FOUND"),
        ],
        ids=["permission", "not_found"],
    )
    async def test_other_terminal_categories_block_from_a_poll(
        self, run_worker, executor, reregister_app, temporal_client, raised, code
    ):
        fake = warming_fake(COLD, raised)
        run = await _run(
            run_worker, executor, reregister_app, temporal_client, HardGateApp, fake
        )
        assert _block_primary(run)["code"] == code
        assert fake.probes == 2
