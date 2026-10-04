"""Unit tests for the scripted warming source (FND-3042)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from application_sdk.errors import FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightInput,
    PreflightStatus,
    WarmupState,
    WarmupStatus,
)
from application_sdk.handler.service import create_app_handler_service
from application_sdk.testing import SourceState, WarmingSource, WarmingSourceHandler
from application_sdk.testing._errors import WarmingScriptEmptyError

_FULL_WARMUP = [
    SourceState.COLD,
    SourceState.WARMING,
    SourceState.QUEUED,
    SourceState.READY,
]


class TestSourceState:
    @pytest.mark.parametrize(
        ("state", "status"),
        [
            (SourceState.COLD, WarmupStatus.NOT_STARTED),
            (SourceState.WARMING, WarmupStatus.RUNNING),
            (SourceState.QUEUED, WarmupStatus.RUNNING),
            (SourceState.READY, WarmupStatus.READY),
            (SourceState.UNAVAILABLE, WarmupStatus.FAILED),
        ],
    )
    def test_maps_onto_warmup_status(
        self, state: SourceState, status: WarmupStatus
    ) -> None:
        assert state.warmup_status is status

    def test_every_state_is_mapped(self) -> None:
        for state in SourceState:
            assert isinstance(state.warmup_status, WarmupStatus)


class TestWarmingSource:
    def test_an_empty_script_is_refused(self) -> None:
        with pytest.raises(WarmingScriptEmptyError):
            WarmingSource([])

    def test_a_cold_source_stays_cold_until_started(self) -> None:
        source = WarmingSource(_FULL_WARMUP)
        for _ in range(3):
            assert source.poll().status is WarmupStatus.NOT_STARTED
        assert source.started is False
        assert source.current is SourceState.COLD

    def test_walks_the_script_one_state_per_call(self) -> None:
        source = WarmingSource(_FULL_WARMUP)
        assert source.start().message == "WARMING"
        assert source.poll().message == "QUEUED"
        assert source.poll().status is WarmupStatus.READY
        assert source.reported == [
            WarmupStatus.RUNNING,
            WarmupStatus.RUNNING,
            WarmupStatus.READY,
        ]
        assert source.calls == ["start", "poll", "poll"]
        assert source.polls == 2

    def test_the_last_state_repeats(self) -> None:
        source = WarmingSource([SourceState.COLD, SourceState.WARMING])
        source.start()
        for _ in range(5):
            assert source.poll().status is WarmupStatus.RUNNING

    def test_a_second_start_reports_without_advancing(self) -> None:
        source = WarmingSource(_FULL_WARMUP)
        assert source.start().message == "WARMING"
        assert source.start().message == "WARMING"
        assert source.current is SourceState.WARMING

    def test_unavailable_is_a_typed_failure(self) -> None:
        source = WarmingSource(
            [SourceState.COLD, SourceState.WARMING, SourceState.UNAVAILABLE]
        )
        source.start()
        state = source.poll()
        assert state.status is WarmupStatus.FAILED
        assert state.error is not None
        assert state.error.code == "SOURCE_UNAVAILABLE"
        assert state.message == state.error.message

    def test_pending_checks_are_reported_until_ready(self) -> None:
        source = WarmingSource(
            [SourceState.COLD, SourceState.WARMING, SourceState.READY],
            pending_checks=["catalogScan"],
        )
        assert source.poll().pending_checks == ["catalogScan"]
        assert source.start().pending_checks == ["catalogScan"]
        assert source.poll().pending_checks == []

    def test_an_exception_step_is_raised_and_not_reported(self) -> None:
        source = WarmingSource(
            [SourceState.COLD, RuntimeError("socket reset"), SourceState.READY]
        )
        with pytest.raises(RuntimeError, match="socket reset"):
            source.start()
        assert source.poll().status is WarmupStatus.READY
        assert source.reported == [WarmupStatus.READY]
        assert source.calls == ["start", "poll"]

    def test_a_literal_warmup_state_is_returned_as_is(self) -> None:
        literal = WarmupState(status=WarmupStatus.NOT_REQUIRED, message="always warm")
        source = WarmingSource([SourceState.COLD, literal])
        assert source.start() is literal


class TestWarmingSourceHandler:
    async def test_hooks_drive_the_source(self) -> None:
        handler = WarmingSourceHandler(WarmingSource(_FULL_WARMUP))
        assert (await handler.warmup_state(PreflightInput())).status is (
            WarmupStatus.NOT_STARTED
        )
        assert (await handler.warmup_start(PreflightInput())).status is (
            WarmupStatus.RUNNING
        )
        assert handler.source.calls == ["poll", "start"]

    async def test_answers_both_tiers_and_records_the_tier_asked(self) -> None:
        handler = WarmingSourceHandler(WarmingSource([SourceState.READY]))
        output = await handler.preflight_check(PreflightInput(tier=CheckTier.FAST))
        await handler.preflight_check(PreflightInput())
        assert output.status is PreflightStatus.READY
        assert [(c.name, c.tier) for c in output.checks] == [
            ("reachable", CheckTier.FAST),
            ("catalogScan", CheckTier.WARMUP),
        ]
        assert handler.check_tiers == ["fast", "all"]

    async def test_a_failing_warmup_check_is_typed_and_not_ready(self) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([SourceState.READY]), warmup_check_passes=False
        )
        output = await handler.preflight_check(PreflightInput(tier=CheckTier.WARMUP))
        assert output.status is PreflightStatus.NOT_READY
        failed = output.checks[1]
        assert failed.passed is False
        assert failed.error is not None
        assert failed.error.category is FailureCategory.PRECONDITION
        assert failed.resolved_message == "catalog scan found no schemas"

    async def test_a_fast_request_stays_ready_when_the_warmup_check_fails(
        self,
    ) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([SourceState.READY]), warmup_check_passes=False
        )
        output = await handler.preflight_check(PreflightInput(tier=CheckTier.FAST))
        assert output.status is PreflightStatus.READY


class TestThroughTheHandlerService:
    """The UI's sequence against the real routes, with the source scripted."""

    def test_warmup_tier_is_refused_until_the_source_is_ready(self) -> None:
        source = WarmingSource(_FULL_WARMUP, pending_checks=["catalogScan"])
        client = TestClient(
            create_app_handler_service(
                WarmingSourceHandler(source), app_name="test-app"
            )
        )
        body = {"credentials": []}

        started = client.post("/workflows/v1/warmup", json=body)
        assert started.status_code == 202
        assert started.json()["data"]["message"] == "WARMING"

        # /check consults warmup_state, which moves the script on: QUEUED.
        early = client.post("/workflows/v1/check", json={**body, "tier": "warmup"})
        assert early.status_code == 412
        assert early.json()["preflight"]["warmup"]["pending_checks"] == ["catalogScan"]

        ready = client.post("/workflows/v1/check", json={**body, "tier": "warmup"})
        assert ready.status_code == 200
        names = [c["name"] for c in ready.json()["preflight"]["checks"]]
        assert names == ["catalogScan"]
        assert source.calls == ["start", "poll", "poll"]

    def test_a_typed_raise_from_the_source_is_its_http_status(self) -> None:
        source = WarmingSource([SourceState.COLD, AuthError(message="token expired")])
        client = TestClient(
            create_app_handler_service(
                WarmingSourceHandler(source), app_name="test-app"
            )
        )
        response = client.post("/workflows/v1/warmup", json={"credentials": []})
        assert response.status_code == 401
