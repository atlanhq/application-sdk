"""Unit tests for the scripted warming source (FND-3237)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from application_sdk.errors import FailureCategory
from application_sdk.errors.leaves import AuthError
from application_sdk.handler.contracts import (
    ALL_CHECK_TIERS,
    CheckTier,
    PreflightInput,
    PreflightStatus,
    WarmupInput,
    WarmupObservation,
    WarmupState,
)
from application_sdk.handler.service import create_app_handler_service
from application_sdk.testing import WarmingSource, WarmingSourceHandler
from application_sdk.testing._errors import WarmingScriptEmptyError

_FULL_WARMUP = [
    WarmupState.COLD,
    WarmupState.WARMING,
    WarmupState.QUEUED,
    WarmupState.READY,
]

_PREFLIGHT_ONLY = PreflightInput(tiers=frozenset({CheckTier.PREFLIGHT}))
_WARMUP_ONLY = PreflightInput(tiers=frozenset({CheckTier.WARMUP}))


class TestWarmingSource:
    def test_an_empty_script_is_refused(self) -> None:
        with pytest.raises(WarmingScriptEmptyError):
            WarmingSource([])

    def test_walks_the_script_one_step_per_probe(self) -> None:
        source = WarmingSource(_FULL_WARMUP)
        assert [source.probe().state for _ in range(4)] == _FULL_WARMUP
        assert source.reported == _FULL_WARMUP
        assert source.probes == 4

    def test_a_state_step_reports_its_name_as_the_source_state(self) -> None:
        observation = WarmingSource([WarmupState.QUEUED]).probe()
        assert observation == WarmupObservation(
            state=WarmupState.QUEUED, source_state="QUEUED"
        )

    def test_the_last_step_repeats(self) -> None:
        source = WarmingSource([WarmupState.COLD, WarmupState.WARMING])
        source.probe()
        for _ in range(5):
            assert source.probe().state is WarmupState.WARMING
        assert source.current is WarmupState.WARMING

    def test_current_is_the_step_the_next_probe_answers(self) -> None:
        source = WarmingSource(_FULL_WARMUP)
        assert source.current is WarmupState.COLD
        source.probe()
        assert source.current is WarmupState.WARMING

    def test_an_observation_step_is_returned_as_is(self) -> None:
        scripted = WarmupObservation(
            state=WarmupState.QUEUED,
            source_state="RESUMING",
            queued_queries=4,
            next_poll_seconds=20,
        )
        source = WarmingSource([scripted])
        assert source.probe() is scripted
        assert source.reported == [WarmupState.QUEUED]

    def test_an_exception_step_is_raised_and_not_reported(self) -> None:
        source = WarmingSource(
            [WarmupState.COLD, RuntimeError("socket reset"), WarmupState.READY]
        )
        source.probe()
        with pytest.raises(RuntimeError, match="socket reset"):
            source.probe()
        assert source.probe().state is WarmupState.READY
        assert source.reported == [WarmupState.COLD, WarmupState.READY]
        assert source.probes == 3


class TestWarmingSourceHandler:
    async def test_warmup_probes_the_source(self) -> None:
        handler = WarmingSourceHandler(WarmingSource(_FULL_WARMUP))
        assert (await handler.warmup(WarmupInput())).state is WarmupState.COLD
        assert (await handler.warmup(WarmupInput())).state is WarmupState.WARMING
        assert handler.calls == ["probe", "probe"]
        assert handler.probes == 2
        assert handler.source.probes == 2

    async def test_every_tier_answers_both_rows(self) -> None:
        handler = WarmingSourceHandler(WarmingSource([WarmupState.READY]))
        output = await handler.preflight_check(PreflightInput())
        assert output.status is PreflightStatus.READY
        assert [(c.name, c.tier) for c in output.checks] == [
            ("reachable", CheckTier.PREFLIGHT),
            ("catalogScan", CheckTier.WARMUP),
        ]
        assert handler.check_tiers == ["preflight+warmup"]

    async def test_answers_only_the_requested_tiers(self) -> None:
        handler = WarmingSourceHandler(WarmingSource([WarmupState.READY]))
        preflight = await handler.preflight_check(_PREFLIGHT_ONLY)
        warmup = await handler.preflight_check(_WARMUP_ONLY)
        assert [c.name for c in preflight.checks] == ["reachable"]
        assert [c.name for c in warmup.checks] == ["catalogScan"]
        assert handler.check_tiers == ["preflight", "warmup"]
        assert handler.calls == ["check:preflight", "check:warmup"]
        assert handler.probes == 0

    async def test_a_failing_warmup_check_is_typed_and_not_ready(self) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([WarmupState.READY]),
            warmup_check_passes=False,
            warmup_check_message="no warehouse grant",
        )
        output = await handler.preflight_check(_WARMUP_ONLY)
        assert output.status is PreflightStatus.NOT_READY
        failed = output.checks[0]
        assert failed.passed is False
        assert failed.error is not None
        assert failed.error.category is FailureCategory.PRECONDITION
        assert failed.resolved_message == "no warehouse grant"

    async def test_a_preflight_request_stays_ready_when_the_warmup_check_fails(
        self,
    ) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([WarmupState.READY]), warmup_check_passes=False
        )
        output = await handler.preflight_check(_PREFLIGHT_ONLY)
        assert output.status is PreflightStatus.READY

    async def test_ignores_tiers_answers_both_rows_whatever_was_asked(
        self,
    ) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([WarmupState.READY]), ignores_tiers=True
        )
        output = await handler.preflight_check(_PREFLIGHT_ONLY)
        assert {c.tier for c in output.checks} == ALL_CHECK_TIERS
        assert handler.check_tiers == ["preflight"]


class TestThroughTheHandlerService:
    """The UI's sequence against the real routes, with the source scripted."""

    @staticmethod
    def _client(handler: WarmingSourceHandler) -> TestClient:
        return TestClient(create_app_handler_service(handler, app_name="test-app"))

    def test_the_warmup_tier_is_pending_until_the_source_is_ready(self) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([WarmupState.COLD, WarmupState.WARMING, WarmupState.READY])
        )
        client = self._client(handler)
        body = {"credentials": [], "tiers": ["preflight", "warmup"]}

        probed = client.post("/workflows/v1/warmup", json={"credentials": []})
        assert probed.json()["data"]["state"] == "cold"

        early = client.post("/workflows/v1/check", json=body).json()
        assert early["preflight"]["status"] == "pending"
        assert early["preflight"]["warmup"]["source_state"] == "WARMING"
        assert [c["name"] for c in early["preflight"]["checks"]] == ["reachable"]

        ready = client.post("/workflows/v1/check", json=body).json()
        assert ready["preflight"]["status"] == "ready"
        assert [c["name"] for c in ready["preflight"]["checks"]] == [
            "reachable",
            "catalogScan",
        ]
        assert handler.calls == [
            "probe",
            "probe",
            "check:preflight",
            "probe",
            "check:preflight+warmup",
        ]

    def test_a_preflight_only_check_never_probes(self) -> None:
        handler = WarmingSourceHandler(WarmingSource([WarmupState.COLD]))
        body = (
            self._client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["preflight"]},
            )
            .json()
        )
        assert body["preflight"]["status"] == "pending"
        assert body["preflight"]["warmup"] is None
        assert handler.calls == ["check:preflight"]

    def test_an_unavailable_source_is_not_ready(self) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([WarmupState.WARMING, WarmupState.UNAVAILABLE])
        )
        client = self._client(handler)
        client.post("/workflows/v1/warmup", json={"credentials": []})
        body = client.post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["warmup"]}
        ).json()
        assert body["preflight"]["status"] == "not_ready"
        assert body["preflight"]["warmup"]["state"] == "unavailable"
        assert handler.check_tiers == []

    def test_a_handler_ignoring_tiers_is_caught_by_the_post_call_check(
        self,
    ) -> None:
        handler = WarmingSourceHandler(
            WarmingSource([WarmupState.READY]), ignores_tiers=True
        )
        response = self._client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["preflight"]}
        )
        assert response.status_code == 500
        assert response.json()["preflight"]["status"] == "not_ready"

    def test_a_typed_raise_from_the_source_is_its_http_status(self) -> None:
        source = WarmingSource([AuthError(message="token expired")])
        response = self._client(WarmingSourceHandler(source)).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 401
