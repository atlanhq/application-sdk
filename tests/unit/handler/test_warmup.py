"""Unit tests for check tiers and the optional handler warmup (FND-3038)."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from application_sdk.errors.leaves import AuthError, SourceUnavailableError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.context import HandlerContext
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupState,
    WarmupStatus,
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
    """An app written before warmup existed: no tiers, no warmup overrides."""

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


class _TieredHandler(DefaultHandler):
    """Returns one check per tier (plus an untiered one) and ignores ``input.tier``."""

    def __init__(self, warmup: WarmupState | None = None) -> None:
        self.warmup = warmup or WarmupState()
        self.preflight_inputs: list[PreflightInput] = []
        self.state_calls = 0
        self.start_calls = 0

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_inputs.append(input)
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(name="untiered", passed=True),
                PreflightCheck(name="reach", passed=True, tier=CheckTier.FAST),
                PreflightCheck(name="scan", passed=True, tier=CheckTier.WARMUP),
            ],
        )

    async def warmup_state(self, input: PreflightInput) -> WarmupState:
        self.state_calls += 1
        return self.warmup

    async def warmup_start(self, input: PreflightInput) -> WarmupState:
        self.start_calls += 1
        return self.warmup


def _client(handler: DefaultHandler) -> TestClient:
    return TestClient(create_app_handler_service(handler, app_name="test-app"))


def _check_names(body: dict) -> list[str]:
    return [check["name"] for check in body["preflight"]["checks"]]


_RUNNING = WarmupState(
    status=WarmupStatus.RUNNING,
    message="resuming warehouse",
    pending_checks=["scan"],
    estimated_duration_ms=90_000.0,
)


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class TestContracts:
    def test_untiered_check_keeps_its_pre_tier_wire_shape(self) -> None:
        check = PreflightCheck(name="c", passed=True)
        assert check.effective_tier is CheckTier.FAST
        assert "tier" not in check.to_wire()
        assert "tier" not in check.model_dump(mode="json", exclude_none=True)

    def test_tiered_check_serializes_its_tier(self) -> None:
        check = PreflightCheck(name="c", passed=True, tier=CheckTier.WARMUP)
        assert check.to_wire()["tier"] == "warmup"
        assert check.effective_tier is CheckTier.WARMUP

    def test_preflight_input_tier_defaults_to_all_and_parses_wire_value(
        self,
    ) -> None:
        assert PreflightInput().tier is None
        assert PreflightInput.model_validate({"tier": "warmup"}).tier is (
            CheckTier.WARMUP
        )

    def test_warmup_state_defaults_to_not_required(self) -> None:
        state = WarmupState()
        assert state.status is WarmupStatus.NOT_REQUIRED
        assert not state.status.is_pending

    @pytest.mark.parametrize(
        ("status", "pending"),
        [
            (WarmupStatus.NOT_REQUIRED, False),
            (WarmupStatus.NOT_STARTED, True),
            (WarmupStatus.RUNNING, True),
            (WarmupStatus.READY, False),
            (WarmupStatus.FAILED, True),
        ],
    )
    def test_is_pending(self, status: WarmupStatus, pending: bool) -> None:
        assert status.is_pending is pending

    def test_failed_warmup_coerces_app_error_and_its_message_wins(self) -> None:
        state = WarmupState(
            status=WarmupStatus.FAILED,
            message="stale",
            error=SourceUnavailableError(message="warehouse suspended"),  # type: ignore[arg-type]
        )
        assert state.error is not None
        assert state.message == "warehouse suspended"


class TestHandlerDefaults:
    async def test_default_handler_has_no_warmup(self) -> None:
        handler = DefaultHandler()
        assert (await handler.warmup_start(PreflightInput())).status is (
            WarmupStatus.NOT_REQUIRED
        )
        assert (await handler.warmup_state(PreflightInput())).status is (
            WarmupStatus.NOT_REQUIRED
        )


# ---------------------------------------------------------------------------
# /workflows/v1/check
# ---------------------------------------------------------------------------


class TestCheckWithoutWarmup:
    def test_untiered_check_is_byte_identical_to_pre_warmup_sdk(self) -> None:
        response = _client(_NoWarmupHandler()).post(
            "/workflows/v1/check", json={"credentials": []}
        )
        assert response.status_code == 200
        assert response.content == _PRE_WARMUP_CHECK_BODY

    def test_fast_tier_on_an_app_without_warmup_adds_no_warmup_key(self) -> None:
        # Every untiered row is FAST, so the fast tier is the whole verdict.
        response = _client(_NoWarmupHandler()).post(
            "/workflows/v1/check", json={"credentials": [], "tier": "fast"}
        )
        assert response.status_code == 200
        assert response.content == _PRE_WARMUP_CHECK_BODY

    def test_untiered_check_never_consults_warmup(self) -> None:
        handler = _TieredHandler(warmup=_RUNNING)
        body = (
            _client(handler)
            .post("/workflows/v1/check", json={"credentials": []})
            .json()
        )
        assert handler.state_calls == 0
        assert "warmup" not in body["preflight"]
        assert _check_names(body) == ["untiered", "reach", "scan"]


class TestCheckTierFilter:
    def test_fast_tier_keeps_fast_and_untiered_rows(self) -> None:
        handler = _TieredHandler()
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tier": "fast"}
        )
        assert response.status_code == 200
        body = response.json()
        assert _check_names(body) == ["untiered", "reach"]
        assert set(body["data"]) == {"untiered", "reach"}
        assert handler.preflight_inputs[0].tier is CheckTier.FAST

    def test_warmup_tier_keeps_only_warmup_rows_when_no_warmup_is_needed(
        self,
    ) -> None:
        handler = _TieredHandler()
        body = (
            _client(handler)
            .post("/workflows/v1/check", json={"credentials": [], "tier": "warmup"})
            .json()
        )
        assert _check_names(body) == ["scan"]
        assert body["preflight"]["checks"][0]["tier"] == "warmup"
        assert "warmup" not in body["preflight"]

    def test_unknown_tier_is_a_422(self) -> None:
        response = _client(_TieredHandler()).post(
            "/workflows/v1/check", json={"credentials": [], "tier": "slow"}
        )
        assert response.status_code == 422


class TestCheckReportsWarmup:
    def test_fast_tier_reports_pending_warmup_checks(self) -> None:
        handler = _TieredHandler(warmup=_RUNNING)
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tier": "fast"}
        )
        assert response.status_code == 200
        body = response.json()
        assert _check_names(body) == ["untiered", "reach"]
        assert body["preflight"]["warmup"] == {
            "status": "running",
            "message": "resuming warehouse",
            "pending_checks": ["scan"],
            "estimated_duration_ms": 90000.0,
        }

    @pytest.mark.parametrize(
        "status",
        [WarmupStatus.NOT_STARTED, WarmupStatus.RUNNING, WarmupStatus.FAILED],
    )
    def test_warmup_tier_is_refused_until_ready(self, status: WarmupStatus) -> None:
        handler = _TieredHandler(
            warmup=WarmupState(status=status, pending_checks=["scan"])
        )
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tier": "warmup"}
        )
        assert response.status_code == 412
        body = response.json()
        assert handler.preflight_inputs == []
        assert body["success"] is False
        assert body["preflight"]["status"] == "not_ready"
        assert body["error"]["category"] == "PRECONDITION"
        assert body["error"]["evidence"]["actual_state"] == status.value
        assert body["preflight"]["warmup"]["status"] == status.value
        assert body["preflight"]["warmup"]["pending_checks"] == ["scan"]

    def test_warmup_tier_runs_once_ready(self) -> None:
        handler = _TieredHandler(warmup=WarmupState(status=WarmupStatus.READY))
        response = _client(handler).post(
            "/workflows/v1/check", json={"credentials": [], "tier": "warmup"}
        )
        assert response.status_code == 200
        body = response.json()
        assert _check_names(body) == ["scan"]
        assert body["preflight"]["warmup"]["status"] == "ready"

    def test_failed_warmup_error_drops_cause_repr(self) -> None:
        failed = WarmupState(
            status=WarmupStatus.FAILED,
            error=SourceUnavailableError(  # type: ignore[arg-type]
                message="warehouse suspended",
                cause=RuntimeError("host=db.internal.example"),
            ),
        )
        body = (
            _client(_TieredHandler(warmup=failed))
            .post("/workflows/v1/check", json={"credentials": [], "tier": "fast"})
            .json()
        )
        assert body["preflight"]["warmup"]["message"] == "warehouse suspended"
        assert "cause_repr" not in body["preflight"]["warmup"]["error"]
        assert "db.internal" not in str(body)


# ---------------------------------------------------------------------------
# /workflows/v1/warmup and /workflows/v1/warmup/state
# ---------------------------------------------------------------------------


class TestWarmupRoutes:
    @pytest.mark.parametrize(
        "path", ["/workflows/v1/warmup", "/workflows/v1/warmup/state"]
    )
    def test_app_without_warmup_reports_not_required(self, path: str) -> None:
        response = _client(_NoWarmupHandler()).post(path, json={"credentials": []})
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == {
            "status": "not_required",
            "message": "",
            "pending_checks": [],
        }

    def test_start_accepts_and_reports_running(self) -> None:
        handler = _TieredHandler(warmup=_RUNNING)
        response = _client(handler).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 202
        body = response.json()
        assert body["success"] is True
        assert body["message"] == "resuming warehouse"
        assert body["data"]["status"] == "running"
        assert handler.start_calls == 1
        assert handler.state_calls == 0

    def test_state_reports_ready(self) -> None:
        handler = _TieredHandler(warmup=WarmupState(status=WarmupStatus.READY))
        response = _client(handler).post(
            "/workflows/v1/warmup/state", json={"credentials": []}
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "ready"
        assert handler.state_calls == 1
        assert handler.start_calls == 0

    def test_failed_state_is_unsuccessful_without_cause_repr(self) -> None:
        failed = WarmupState(
            status=WarmupStatus.FAILED,
            error=SourceUnavailableError(  # type: ignore[arg-type]
                message="warehouse suspended",
                cause=RuntimeError("host=db.internal.example"),
            ),
        )
        response = _client(_TieredHandler(warmup=failed)).post(
            "/workflows/v1/warmup/state", json={"credentials": []}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is False
        assert body["message"] == "warehouse suspended"
        assert "cause_repr" not in body["data"]["error"]

    def test_warmup_uses_the_check_request_shape(self) -> None:
        seen: list[PreflightInput] = []

        class _Capture(_TieredHandler):
            async def warmup_start(self, input: PreflightInput) -> WarmupState:
                seen.append(input)
                return WarmupState(status=WarmupStatus.READY)

        _client(_Capture()).post(
            "/workflows/v1/warmup",
            json={"credentials": {"host": "h"}, "metadata": {"warehouse": "w"}},
        )
        assert [c.key for c in seen[0].credentials] == ["host"]
        assert seen[0].connection_config.get("warehouse") == "w"

    def test_typed_raise_maps_to_its_http_status(self) -> None:
        class _Raises(_TieredHandler):
            async def warmup_start(self, input: PreflightInput) -> WarmupState:
                raise AuthError(message="bad creds")

        response = _client(_Raises()).post(
            "/workflows/v1/warmup", json={"credentials": []}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "bad creds"

    def test_untyped_raise_is_a_500_without_exception_text(self) -> None:
        class _Crashes(_TieredHandler):
            async def warmup_state(self, input: PreflightInput) -> WarmupState:
                raise RuntimeError("host=db.internal.example")

        response = _client(_Crashes()).post(
            "/workflows/v1/warmup/state", json={"credentials": []}
        )
        assert response.status_code == 500
        assert "db.internal" not in response.text

    def test_malformed_entrypoint_is_a_400(self) -> None:
        response = _client(_TieredHandler()).post(
            "/workflows/v1/warmup", json={"credentials": [], "entrypoint": "../x"}
        )
        assert response.status_code == 400


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

    def test_module_hooks_preempt_the_app_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def warmup_start(
            input: PreflightInput, ctx: HandlerContext
        ) -> WarmupState:
            return WarmupState(status=WarmupStatus.RUNNING, message="ep start")

        async def warmup_state(
            input: PreflightInput, ctx: HandlerContext
        ) -> WarmupState:
            return WarmupState(status=WarmupStatus.RUNNING, pending_checks=["ep"])

        self._install(monkeypatch, warmup_start=warmup_start, warmup_state=warmup_state)
        handler = _TieredHandler()
        client = _client(handler)
        request = {"credentials": [], "entrypoint": "warm-ep"}

        start = client.post("/workflows/v1/warmup", json=request)
        assert start.json()["message"] == "ep start"

        check = client.post("/workflows/v1/check", json={**request, "tier": "fast"})
        assert check.json()["preflight"]["warmup"]["pending_checks"] == ["ep"]
        assert handler.start_calls == 0
        assert handler.state_calls == 0

    def test_missing_hook_falls_through_to_the_app_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._install(monkeypatch)
        handler = _TieredHandler(warmup=_RUNNING)
        response = _client(handler).post(
            "/workflows/v1/warmup/state",
            json={"credentials": [], "entrypoint": "warm-ep"},
        )
        assert response.json()["data"]["status"] == "running"
        assert handler.state_calls == 1
