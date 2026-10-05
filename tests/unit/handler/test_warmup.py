"""Unit tests for check tiers and the optional handler warmup probe (FND-3237)."""

from __future__ import annotations

import asyncio
import math
import sys
import time
import types
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

import orjson
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError, SourceUnavailableError
from application_sdk.handler._preflight_outcome import (
    _check_matrix_json,
    rows_outside_tiers,
)
from application_sdk.handler._warmup import (
    WARMUP_CEILING_DEFAULT_SECONDS,
    WARMUP_CEILING_MIN_SECONDS,
    WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS,
    bounded_warmup_probe,
    warmup_ceiling_seconds,
    warmup_poll_delay,
    warmup_probe_timeout_seconds,
    warmup_unavailable_error,
)
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    ALL_CHECK_TIERS,
    CheckTier,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupInput,
    WarmupObservation,
    WarmupState,
)
from application_sdk.handler.service import create_app_handler_service

# The exact ``/check`` body the SDK served for ``_NoWarmupHandler`` before
# tiers and warmup existed, captured from the parent commit. An app that never
# opts in must keep receiving these bytes.
_PRE_WARMUP_CHECK_BODY = (
    b'{"success":true,"data":{"connectivity":{"success":true,"message":"ok",'
    b'"successMessage":"ok","failureMessage":""},"permissions":{"success":false,'
    b'"message":"denied","successMessage":"","failureMessage":"denied"}},'
    b'"message":"m","preflight":{"status":"not_ready","message":"m",'
    b'"total_duration_ms":3.5,"checks":[{"name":"Connectivity","passed":true,'
    b'"message":"ok","duration_ms":1.5},{"name":"permissions","passed":false,'
    b'"message":"denied","error":{"category":"AUTH","code":"AUTH",'
    b'"retryable":false,"audience":"USER","message":"denied",'
    b'"suggested_action":"grant it","evidence":{"auth_method":null,'
    b'"principal":null,"failure_reason":null}},"suggested_action":"grant it"}]}}'
)


class _NoWarmupHandler(DefaultHandler):
    """An app written before warmup existed: no tiers, no warmup override."""

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.NOT_READY,
            message="m",
            total_duration_ms=3.5,
            checks=[
                PreflightCheck(
                    name="Connectivity", passed=True, message="ok", duration_ms=1.5
                ),
                PreflightCheck(
                    name="permissions",
                    passed=False,
                    error=AuthError(
                        message="denied", suggested_action="grant it"
                    ).to_failure_details(),
                ),
            ],
        )


class _PlainHandler(DefaultHandler):
    """No warmup override; one passing ``PREFLIGHT`` row."""

    def __init__(self) -> None:
        self.preflight_inputs: list[PreflightInput] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_inputs.append(input)
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="reach", passed=True)],
        )


class _TieredHandler(DefaultHandler):
    """Overrides ``warmup``; answers one row per requested tier.

    ``ignores_tiers`` returns both rows whatever was asked, to exercise the
    post-call tier check. ``reach_passes=False`` fails the ``PREFLIGHT`` row.
    """

    def __init__(
        self,
        observation: WarmupObservation | None = None,
        *,
        ignores_tiers: bool = False,
        reach_passes: bool = True,
    ) -> None:
        self.observation = observation or WarmupObservation(state=WarmupState.READY)
        self.ignores_tiers = ignores_tiers
        self.reach_passes = reach_passes
        self.preflight_inputs: list[PreflightInput] = []
        self.warmup_inputs: list[WarmupInput] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_inputs.append(input)
        wanted = ALL_CHECK_TIERS if self.ignores_tiers else input.tiers
        checks: list[PreflightCheck] = []
        if CheckTier.PREFLIGHT in wanted:
            checks.append(
                PreflightCheck(
                    name="reach",
                    passed=self.reach_passes,
                    error=None
                    if self.reach_passes
                    else AuthError(message="denied").to_failure_details(),
                )
            )
        if CheckTier.WARMUP in wanted:
            checks.append(
                PreflightCheck(name="scan", passed=True, tier=CheckTier.WARMUP)
            )
        return PreflightOutput(
            status=PreflightStatus.READY
            if self.reach_passes
            else PreflightStatus.NOT_READY,
            checks=checks,
        )

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        self.warmup_inputs.append(input)
        return self.observation


class _SlowWarmupHandler(_TieredHandler):
    """A warmup probe that never answers inside the probe timeout."""

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        self.warmup_inputs.append(input)
        await asyncio.sleep(30)
        return WarmupObservation(state=WarmupState.READY)


@dataclass
class _WarmInput(Input, allow_unbounded_fields=True):
    name: str = ""


@dataclass
class _WarmOutput(Output, allow_unbounded_fields=True):
    result: str = ""


@pytest.fixture
def app_registry() -> Iterator[None]:
    """A clean registry for tests that declare an ``App`` subclass."""
    AppRegistry.reset()
    TaskRegistry.reset()
    try:
        yield
    finally:
        AppRegistry.reset()
        TaskRegistry.reset()


def _warm_app(ceiling: int, probe_timeout: int) -> type[App]:
    class _WarmApp(App):
        preflight_warmup_ceiling_seconds: ClassVar[int] = ceiling
        preflight_warmup_probe_timeout_seconds: ClassVar[int] = probe_timeout

        async def run(self, input: _WarmInput) -> _WarmOutput:
            return _WarmOutput()

    return _WarmApp


def _client(handler: DefaultHandler, app_class: type[App] | None = None) -> TestClient:
    return TestClient(
        create_app_handler_service(handler, app_name="test-app", app_class=app_class)
    )


def _check_names(body: dict[str, object]) -> list[str]:
    preflight = body["preflight"]
    assert isinstance(preflight, dict)
    return [check["name"] for check in preflight["checks"]]


_WARMING = WarmupObservation(
    state=WarmupState.WARMING,
    source_state="RESUMING",
    queued_queries=3,
    next_poll_seconds=15,
)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class TestContracts:
    def test_default_tier_is_preflight_and_stays_off_the_wire(self) -> None:
        check = PreflightCheck(name="c", passed=True)
        assert check.tier is CheckTier.PREFLIGHT
        assert "tier" not in check.to_wire()
        assert "tier" not in orjson.loads(_check_matrix_json([check]))[0]

    def test_warmup_tier_is_on_the_wire(self) -> None:
        check = PreflightCheck(name="c", passed=True, tier=CheckTier.WARMUP)
        assert check.to_wire()["tier"] == "warmup"
        assert orjson.loads(_check_matrix_json([check]))[0]["tier"] == "warmup"

    def test_preflight_input_tiers_default_to_every_tier(self) -> None:
        assert PreflightInput().tiers == ALL_CHECK_TIERS
        assert ALL_CHECK_TIERS == frozenset({CheckTier.PREFLIGHT, CheckTier.WARMUP})
        parsed = PreflightInput.model_validate({"tiers": ["warmup"]})
        assert parsed.tiers == frozenset({CheckTier.WARMUP})

    def test_empty_tiers_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            PreflightInput.model_validate({"tiers": []})

    def test_warmup_input_takes_the_check_body_and_a_probe_timeout(self) -> None:
        parsed = WarmupInput.model_validate(
            {"credentials": [{"key": "host", "value": "h"}]}
        )
        assert parsed.probe_timeout_seconds == 10
        assert [c.key for c in parsed.credentials] == ["host"]

    @pytest.mark.parametrize(
        ("state", "pending"),
        [
            (WarmupState.COLD, True),
            (WarmupState.WARMING, True),
            (WarmupState.QUEUED, True),
            (WarmupState.READY, False),
            (WarmupState.UNAVAILABLE, False),
        ],
    )
    def test_is_pending(self, state: WarmupState, pending: bool) -> None:
        assert state.is_pending is pending

    def test_observation_is_frozen(self) -> None:
        observation = WarmupObservation(state=WarmupState.COLD)
        with pytest.raises(ValidationError):
            observation.state = WarmupState.READY  # type: ignore[misc]

    def test_observation_defaults(self) -> None:
        observation = WarmupObservation(state=WarmupState.QUEUED)
        assert observation.source_state == ""
        assert observation.queued_queries is None
        assert observation.next_poll_seconds is None

    @pytest.mark.parametrize("field", ["queued_queries", "next_poll_seconds"])
    def test_observation_counts_are_non_negative(self, field: str) -> None:
        with pytest.raises(ValidationError):
            WarmupObservation.model_validate({"state": "warming", field: -1})

    def test_observation_requires_a_state(self) -> None:
        with pytest.raises(ValidationError):
            WarmupObservation.model_validate({})

    def test_rows_outside_tiers_names_the_strays(self) -> None:
        result = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(name="reach", passed=True),
                PreflightCheck(name="scan", passed=True, tier=CheckTier.WARMUP),
            ],
        )
        assert rows_outside_tiers(result, frozenset({CheckTier.PREFLIGHT})) == ["scan"]
        assert rows_outside_tiers(result, ALL_CHECK_TIERS) == []


class TestHandlerDefaults:
    async def test_default_handler_warmup_is_ready(self) -> None:
        observation = await DefaultHandler().warmup(WarmupInput())
        assert observation.state is WarmupState.READY


# ---------------------------------------------------------------------------
# /workflows/v1/check
# ---------------------------------------------------------------------------


class TestCheckWithoutTiers:
    def test_untiered_check_is_byte_identical_to_pre_warmup_sdk(self) -> None:
        response = _client(_NoWarmupHandler()).post(
            "/workflows/v1/check", json={"credentials": []}
        )
        assert response.status_code == 200
        assert response.content == _PRE_WARMUP_CHECK_BODY

    def test_preflight_tier_on_an_app_without_warmup_is_byte_identical(
        self,
    ) -> None:
        response = _client(_NoWarmupHandler()).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["preflight"]}
        )
        assert response.status_code == 200
        assert response.content == _PRE_WARMUP_CHECK_BODY

    def test_untiered_check_never_probes_and_runs_every_tier(self) -> None:
        handler = _TieredHandler(_WARMING)
        body = (
            _client(handler)
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert handler.warmup_inputs == []
        assert handler.preflight_inputs[0].tiers == ALL_CHECK_TIERS
        assert body["preflight"]["status"] == "ready"
        assert "warmup" not in body["preflight"]
        assert _check_names(body) == ["reach", "scan"]
        assert body["preflight"]["checks"][1]["tier"] == "warmup"
        assert "tier" not in body["preflight"]["checks"][0]

    def test_unknown_tier_is_a_422(self) -> None:
        response = _client(_TieredHandler()).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["slow"]}
        )
        assert response.status_code == 422

    def test_empty_tiers_is_a_422(self) -> None:
        response = _client(_TieredHandler()).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": []}
        )
        assert response.status_code == 422


class TestCheckPreflightTierOnly:
    def test_app_with_warmup_is_pending_without_probing(self) -> None:
        handler = _TieredHandler()
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["preflight"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert handler.warmup_inputs == []
        assert handler.preflight_inputs[0].tiers == frozenset({CheckTier.PREFLIGHT})
        assert body["success"] is True
        assert body["preflight"]["status"] == "pending"
        assert body["preflight"]["warmup"] is None
        assert _check_names(body) == ["reach"]

    def test_app_without_warmup_is_ready(self) -> None:
        handler = _PlainHandler()
        body = (
            _client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["preflight"]},
            )
            .json()
        )
        assert body["preflight"]["status"] == "ready"
        assert "warmup" not in body["preflight"]

    def test_not_ready_wins_over_pending(self) -> None:
        handler = _TieredHandler(reach_passes=False)
        body = (
            _client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["preflight"]},
            )
            .json()
        )
        assert body["preflight"]["status"] == "not_ready"
        assert "warmup" not in body["preflight"]


class TestCheckWarmupTier:
    def test_ready_source_runs_every_requested_tier(self) -> None:
        handler = _TieredHandler()
        response = _client(handler).post(
            "/workflows/v1/check",
            json={"credentials": [], "tiers": ["preflight", "warmup"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(handler.warmup_inputs) == 1
        assert handler.preflight_inputs[0].tiers == ALL_CHECK_TIERS
        assert body["preflight"]["status"] == "ready"
        assert _check_names(body) == ["reach", "scan"]
        # A READY observation explains nothing, so it is not attached.
        assert "warmup" not in body["preflight"]

    def test_warming_source_runs_the_rest_and_is_pending(self) -> None:
        handler = _TieredHandler(_WARMING)
        body = (
            _client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["preflight", "warmup"]},
            )
            .json()
        )
        assert handler.preflight_inputs[0].tiers == frozenset({CheckTier.PREFLIGHT})
        assert body["success"] is True
        assert body["preflight"]["status"] == "pending"
        assert _check_names(body) == ["reach"]
        assert body["preflight"]["warmup"] == {
            "state": "warming",
            "source_state": "RESUMING",
            "queued_queries": 3,
            "next_poll_seconds": 15,
        }

    def test_warmup_only_on_a_warming_source_skips_the_handler(self) -> None:
        handler = _TieredHandler(WarmupObservation(state=WarmupState.QUEUED))
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["warmup"]}
        )
        assert response.status_code == 200
        body = response.json()
        assert handler.preflight_inputs == []
        # Envelope success is "preflight executed", true for PENDING with no rows.
        assert body["success"] is True
        assert body["preflight"]["status"] == "pending"
        assert body["preflight"]["checks"] == []
        assert body["preflight"]["warmup"]["state"] == "queued"

    def test_warmup_only_on_a_ready_source_runs_only_warmup(self) -> None:
        handler = _TieredHandler()
        body = (
            _client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["warmup"]},
            )
            .json()
        )
        assert handler.preflight_inputs[0].tiers == frozenset({CheckTier.WARMUP})
        assert _check_names(body) == ["scan"]
        assert body["preflight"]["status"] == "ready"

    def test_unavailable_source_is_not_ready_attributed_to_the_source(self) -> None:
        observation = WarmupObservation(
            state=WarmupState.UNAVAILABLE, source_state="SUSPENDED"
        )
        handler = _TieredHandler(observation)
        body = (
            _client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["preflight", "warmup"]},
            )
            .json()
        )
        assert body["preflight"]["status"] == "not_ready"
        assert body["preflight"]["message"] == (
            "The source reported its compute as unavailable (SUSPENDED)"
        )
        assert body["preflight"]["warmup"]["state"] == "unavailable"
        assert _check_names(body) == ["reach"]

    def test_not_ready_wins_over_pending_and_keeps_the_observation(self) -> None:
        handler = _TieredHandler(_WARMING, reach_passes=False)
        body = (
            _client(handler)
            .post(
                "/workflows/v1/check",
                json={"credentials": [], "tiers": ["preflight", "warmup"]},
            )
            .json()
        )
        assert body["preflight"]["status"] == "not_ready"
        assert body["preflight"]["warmup"]["state"] == "warming"

    def test_typed_probe_raise_is_a_failure_verdict_with_its_status(self) -> None:
        class _Raises(_TieredHandler):
            async def warmup(self, input: WarmupInput) -> WarmupObservation:
                raise AuthError(message="token expired")

        handler = _Raises()
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["warmup"]}
        )
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["preflight"]["status"] == "not_ready"
        assert body["error"]["category"] == "AUTH"
        assert handler.preflight_inputs == []

    def test_probe_overrun_reads_as_warming(self, app_registry: None) -> None:
        handler = _SlowWarmupHandler()
        client = _client(handler, _warm_app(ceiling=60, probe_timeout=1))
        started = time.monotonic()
        body = client.post(
            "/workflows/v1/check",
            json={"credentials": [], "tiers": ["preflight", "warmup"]},
        ).json()
        assert time.monotonic() - started < 10
        assert handler.warmup_inputs[0].probe_timeout_seconds == 1
        assert body["preflight"]["status"] == "pending"
        assert body["preflight"]["warmup"] == {"state": "warming", "source_state": ""}


class TestPostCallTierCheck:
    def test_a_row_outside_the_requested_tiers_is_an_unverifiable_500(
        self,
    ) -> None:
        handler = _TieredHandler(ignores_tiers=True)
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tiers": ["preflight"]}
        )
        assert response.status_code == 500
        body = response.json()
        assert body["success"] is False
        assert body["preflight"]["status"] == "not_ready"
        assert body["error"]["category"] == "INTERNAL"
        assert "scan" in body["preflight"]["checks"][0]["message"]

    def test_a_warming_source_still_checks_the_rows_that_came_back(self) -> None:
        handler = _TieredHandler(_WARMING, ignores_tiers=True)
        response = _client(handler).post(
            "/workflows/v1/check",
            json={"credentials": [], "tiers": ["preflight", "warmup"]},
        )
        assert response.status_code == 500

    def test_untiered_request_accepts_every_row(self) -> None:
        handler = _TieredHandler(ignores_tiers=True)
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": []}
        )
        assert response.status_code == 200
        assert _check_names(response.json()) == ["reach", "scan"]


# ---------------------------------------------------------------------------
# /workflows/v1/warmup
# ---------------------------------------------------------------------------


class TestWarmupRoute:
    def test_app_without_warmup_reports_ready_and_the_default_ceiling(
        self,
    ) -> None:
        response = _client(_NoWarmupHandler()).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == {
            "state": "ready",
            "source_state": "",
            "ceiling_seconds": WARMUP_CEILING_DEFAULT_SECONDS,
        }

    def test_reports_the_observation_and_the_apps_ceiling(
        self, app_registry: None
    ) -> None:
        handler = _TieredHandler(_WARMING)
        response = _client(handler, _warm_app(ceiling=900, probe_timeout=7)).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == {
            "state": "warming",
            "source_state": "RESUMING",
            "queued_queries": 3,
            "next_poll_seconds": 15,
            "ceiling_seconds": 900,
        }
        assert handler.warmup_inputs[0].probe_timeout_seconds == 7
        assert handler.preflight_inputs == []

    def test_declared_ceiling_is_clamped(self, app_registry: None) -> None:
        body = (
            _client(_TieredHandler(), _warm_app(ceiling=5, probe_timeout=10))
            .post("/workflows/v1/warmup", json={"credentials": []})
            .json()
        )
        assert body["data"]["ceiling_seconds"] == WARMUP_CEILING_MIN_SECONDS

    def test_unavailable_is_unsuccessful(self) -> None:
        handler = _TieredHandler(WarmupObservation(state=WarmupState.UNAVAILABLE))
        response = _client(handler).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["data"]["state"] == "unavailable"

    def test_each_request_probes_once(self) -> None:
        handler = _TieredHandler()
        client = _client(handler)
        for _ in range(3):
            client.post("/workflows/v1/warmup", json={"credentials": []})
        assert len(handler.warmup_inputs) == 3

    def test_warmup_uses_the_check_request_shape(self) -> None:
        handler = _TieredHandler()
        _client(handler).post(
            "/workflows/v1/warmup",
            json={"credentials": {"host": "h"}, "metadata": {"warehouse": "w"}},
        )
        seen = handler.warmup_inputs[0]
        assert [c.key for c in seen.credentials] == ["host"]
        assert seen.connection_config.get("warehouse") == "w"
        assert seen.probe_timeout_seconds == WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS

    def test_probe_overrun_reads_as_warming(self, app_registry: None) -> None:
        client = _client(_SlowWarmupHandler(), _warm_app(ceiling=60, probe_timeout=1))
        started = time.monotonic()
        response = client.post("/workflows/v1/warmup", json={"credentials": []})
        assert time.monotonic() - started < 10
        assert response.status_code == 200
        assert response.json()["data"]["state"] == "warming"

    def test_typed_raise_maps_to_its_http_status(self) -> None:
        class _Raises(_TieredHandler):
            async def warmup(self, input: WarmupInput) -> WarmupObservation:
                raise AuthError(message="bad creds")

        response = _client(_Raises()).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "bad creds"

    def test_untyped_raise_is_a_500_without_exception_text(self) -> None:
        class _Crashes(_TieredHandler):
            async def warmup(self, input: WarmupInput) -> WarmupObservation:
                raise RuntimeError("host=db.internal.example")

        response = _client(_Crashes()).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 500
        assert "db.internal" not in response.text

    def test_malformed_entrypoint_is_a_400(self) -> None:
        response = _client(_TieredHandler()).post(
            "/workflows/v1/warmup", json={"credentials": [], "entrypoint": "../x"}
        )
        assert response.status_code == 400

    def test_the_state_route_is_gone(self) -> None:
        response = _client(_TieredHandler()).post(
            "/workflows/v1/warmup/state", json={"credentials": []}
        )
        assert response.status_code in (404, 405)


class TestPerEntrypointWarmup:
    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, **attrs: object) -> None:
        """Register ``app.warm_ep.handler`` with ``attrs`` set."""
        for parent in ("app", "app.warm_ep"):
            if parent not in sys.modules:
                pkg = types.ModuleType(parent)
                pkg.__path__ = []  # type: ignore[attr-defined]
                monkeypatch.setitem(sys.modules, parent, pkg)
        module = types.ModuleType("app.warm_ep.handler")
        for name, value in attrs.items():
            setattr(module, name, value)
        monkeypatch.setitem(sys.modules, "app.warm_ep.handler", module)

    def test_module_hook_preempts_the_app_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def warmup(input: WarmupInput, ctx: HandlerContext) -> WarmupObservation:
            return WarmupObservation(state=WarmupState.QUEUED, source_state="ep")

        self._install(monkeypatch, warmup=warmup)
        handler = _TieredHandler()
        client = _client(handler)
        request = {"credentials": [], "entrypoint": "warm-ep"}

        probed = client.post("/workflows/v1/warmup", json=request)
        assert probed.json()["data"]["source_state"] == "ep"

        check = client.post(
            "/workflows/v1/check", json={**request, "tiers": ["preflight", "warmup"]}
        )
        assert check.json()["preflight"]["warmup"]["source_state"] == "ep"
        assert check.json()["preflight"]["status"] == "pending"
        assert handler.warmup_inputs == []

    def test_module_hook_makes_a_preflight_only_check_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def warmup(input: WarmupInput, ctx: HandlerContext) -> WarmupObservation:
            return WarmupObservation(state=WarmupState.READY)

        self._install(monkeypatch, warmup=warmup)
        body = (
            _client(_PlainHandler())
            .post(
                "/workflows/v1/check",
                json={
                    "credentials": [],
                    "entrypoint": "warm-ep",
                    "tiers": ["preflight"],
                },
            )
            .json()
        )
        assert body["preflight"]["status"] == "pending"
        assert body["preflight"]["warmup"] is None

    def test_sync_hook_is_not_a_warmup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def warmup(input: WarmupInput, ctx: HandlerContext) -> WarmupObservation:
            return WarmupObservation(state=WarmupState.WARMING)

        self._install(monkeypatch, warmup=warmup)
        body = (
            _client(_PlainHandler())
            .post(
                "/workflows/v1/check",
                json={
                    "credentials": [],
                    "entrypoint": "warm-ep",
                    "tiers": ["preflight"],
                },
            )
            .json()
        )
        assert body["preflight"]["status"] == "ready"

    def test_missing_hook_falls_through_to_the_app_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install(monkeypatch)
        handler = _TieredHandler(_WARMING)
        response = _client(handler).post(
            "/workflows/v1/warmup",
            json={"credentials": [], "entrypoint": "warm-ep"},
        )
        assert response.json()["data"]["state"] == "warming"
        assert len(handler.warmup_inputs) == 1


# ---------------------------------------------------------------------------
# _warmup.py helpers
# ---------------------------------------------------------------------------


class TestWarmupSettings:
    def test_ceiling_defaults(self) -> None:
        assert warmup_ceiling_seconds(None) == (WARMUP_CEILING_DEFAULT_SECONDS, "")

    def test_ceiling_is_floored(self) -> None:
        value, complaint = warmup_ceiling_seconds(10)
        assert value == WARMUP_CEILING_MIN_SECONDS
        assert "at least 30s" in complaint

    def test_ceiling_is_never_capped(self) -> None:
        assert warmup_ceiling_seconds(86_400) == (86_400, "")

    @pytest.mark.parametrize("raw", [True, "abc", math.inf, "1e400", [600]])
    def test_unusable_ceiling_falls_back_to_the_default(self, raw: object) -> None:
        value, complaint = warmup_ceiling_seconds(raw)
        assert value == WARMUP_CEILING_DEFAULT_SECONDS
        assert complaint

    def test_probe_timeout_defaults(self) -> None:
        assert warmup_probe_timeout_seconds(None, 600) == (10, "")

    def test_probe_timeout_is_floored(self) -> None:
        value, complaint = warmup_probe_timeout_seconds(0, 600)
        assert value == 1
        assert complaint

    def test_probe_timeout_never_exceeds_the_ceiling(self) -> None:
        value, complaint = warmup_probe_timeout_seconds(1_000, 45)
        assert value == 45
        assert "1-45s" in complaint

    def test_probe_timeout_default_never_exceeds_the_ceiling(self) -> None:
        assert warmup_probe_timeout_seconds(None, 5) == (5, "")


class TestWarmupPollDelay:
    @pytest.mark.parametrize(
        ("unhinted_polls", "delay"),
        [(0, 5.0), (1, 10.0), (2, 20.0), (3, 30.0), (4, 30.0), (1_000, 30.0)],
    )
    def test_unhinted_backs_off_5s_doubling_to_30s(
        self, unhinted_polls: int, delay: float
    ) -> None:
        assert warmup_poll_delay(unhinted_polls, None, 3_600) == delay

    def test_hint_is_honoured(self) -> None:
        assert warmup_poll_delay(3, 45, 3_600) == 45.0

    @pytest.mark.parametrize("hint", [0, 2])
    def test_hint_is_floored_at_5s(self, hint: int) -> None:
        assert warmup_poll_delay(0, hint, 3_600) == 5.0

    def test_never_past_remaining(self) -> None:
        assert warmup_poll_delay(3, None, 4.5) == 4.5
        assert warmup_poll_delay(0, 120, 7.0) == 7.0

    def test_no_remaining_is_no_wait(self) -> None:
        assert warmup_poll_delay(0, None, -3.0) == 0.0


class TestBoundedWarmupProbe:
    async def test_returns_the_observation(self) -> None:
        async def probe() -> WarmupObservation:
            return _WARMING

        assert await bounded_warmup_probe(probe(), 1.0) is _WARMING

    async def test_a_raise_propagates(self) -> None:
        async def probe() -> WarmupObservation:
            raise AuthError(message="bad creds")

        with pytest.raises(AuthError):
            await bounded_warmup_probe(probe(), 1.0)

    async def test_overrun_returns_none_even_when_the_probe_swallows_cancel(
        self,
    ) -> None:
        cancelled = asyncio.Event()
        finished = asyncio.Event()

        async def probe() -> WarmupObservation:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                # Swallow the cancel and keep running past the bound.
                await asyncio.sleep(0.2)
            finished.set()
            return WarmupObservation(state=WarmupState.READY)

        started = time.monotonic()
        assert await bounded_warmup_probe(probe(), 0.05) is None
        assert time.monotonic() - started < 0.15
        assert not finished.is_set()
        await asyncio.wait_for(finished.wait(), timeout=5)
        assert cancelled.is_set()


class TestWarmupUnavailableError:
    def test_names_the_source_state_and_queue(self) -> None:
        error = warmup_unavailable_error(
            WarmupObservation(
                state=WarmupState.UNAVAILABLE,
                source_state="SUSPENDED",
                queued_queries=2,
            ),
            "test-app",
        )
        assert isinstance(error, SourceUnavailableError)
        assert error.category is FailureCategory.SOURCE_UNAVAILABLE
        assert error.message == (
            "The source reported its compute as unavailable (SUSPENDED, 2 queued)"
        )
        assert error.retryable is True

    def test_falls_back_to_the_state(self) -> None:
        error = warmup_unavailable_error(
            WarmupObservation(state=WarmupState.UNAVAILABLE), "test-app"
        )
        assert error.message.endswith("(unavailable)")
