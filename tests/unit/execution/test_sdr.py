"""Unit tests for SDR Temporal workflows and activity wiring."""

from __future__ import annotations

from unittest import mock

import pytest

from application_sdk.execution._temporal.sdr import (
    SDR_FETCH_METADATA_ACTIVITY,
    SDR_PREFLIGHT_ACTIVITY,
    SDR_TEST_AUTH_ACTIVITY,
    SDR_WORKFLOWS,
    SdrFetchMetadataWorkflow,
    SdrPreflightCheckWorkflow,
    SdrTestAuthWorkflow,
    build_sdr_activities,
)
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataOutput,
)


class _StubHandler(Handler):
    """Handler that records context at call time and returns canned outputs."""

    def __init__(self) -> None:
        super().__init__()
        self.auth_input: AuthInput | None = None
        self.preflight_input: PreflightInput | None = None
        self.metadata_input: MetadataInput | None = None
        self.context_during_call: object | None = None

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        self.auth_input = input
        self.context_during_call = self._context
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_input = input
        self.context_during_call = self._context
        return PreflightOutput(status=PreflightStatus.READY, checks=[])

    async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
        self.metadata_input = input
        self.context_during_call = self._context
        return SqlMetadataOutput(objects=[])


class TestSdrWorkflows:
    """SDR workflow classes themselves are static — verify naming and membership."""

    def test_workflow_names(self) -> None:
        # Temporal stores the registered name under __temporal_workflow_definition.name
        defn = getattr(SdrTestAuthWorkflow, "__temporal_workflow_definition")
        assert defn.name == "sdr:test_auth"
        defn = getattr(SdrPreflightCheckWorkflow, "__temporal_workflow_definition")
        assert defn.name == "sdr:preflight_check"
        defn = getattr(SdrFetchMetadataWorkflow, "__temporal_workflow_definition")
        assert defn.name == "sdr:fetch_metadata"

    def test_sdr_workflows_constant(self) -> None:
        assert len(SDR_WORKFLOWS) == 3
        assert SdrTestAuthWorkflow in SDR_WORKFLOWS
        assert SdrPreflightCheckWorkflow in SDR_WORKFLOWS
        assert SdrFetchMetadataWorkflow in SDR_WORKFLOWS


class TestBuildSdrActivities:
    """Tests for build_sdr_activities()."""

    def test_returns_three_activities_with_sdr_names(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        assert len(activities) == 3
        names = [getattr(a, "__temporal_activity_definition").name for a in activities]
        assert set(names) == {
            SDR_TEST_AUTH_ACTIVITY,
            SDR_PREFLIGHT_ACTIVITY,
            SDR_FETCH_METADATA_ACTIVITY,
        }

    @pytest.mark.asyncio
    async def test_test_auth_activity_dispatches_and_sets_context(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        input_obj = AuthInput(credentials=[], connection_id="c1")
        result = await test_auth(input_obj)

        assert result.status == AuthStatus.SUCCESS
        assert handler.auth_input is input_obj
        # Context was set during the call, and cleared afterwards.
        assert handler.context_during_call is not None
        assert handler._context is None

    @pytest.mark.asyncio
    async def test_preflight_activity_dispatches(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        preflight = by_name[SDR_PREFLIGHT_ACTIVITY]

        input_obj = PreflightInput(credentials=[], connection_config={"host": "x"})
        result = await preflight(input_obj)

        assert result.status == PreflightStatus.READY
        assert handler.preflight_input is input_obj
        assert handler._context is None

    @pytest.mark.asyncio
    async def test_fetch_metadata_activity_dispatches(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        fetch_metadata = by_name[SDR_FETCH_METADATA_ACTIVITY]

        input_obj = MetadataInput(credentials=[], connection_config={})
        result = await fetch_metadata(input_obj)

        assert result.objects == []
        assert handler.metadata_input is input_obj
        assert handler._context is None

    @pytest.mark.asyncio
    async def test_context_app_name_and_credentials_are_populated(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        from application_sdk.handler.contracts import HandlerCredential

        creds = [HandlerCredential(key="api_key", value="secret123")]
        await test_auth(AuthInput(credentials=creds))

        captured = handler.context_during_call
        assert captured is not None
        assert captured.app_name == "myapp"  # type: ignore[attr-defined]
        assert len(captured.credentials) == 1  # type: ignore[attr-defined]
        assert captured.get_credential("api_key") == "secret123"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_context_clears_even_when_handler_raises(self) -> None:
        class _FailingHandler(Handler):
            async def test_auth(self, input: AuthInput) -> AuthOutput:
                raise RuntimeError("boom")

            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return PreflightOutput(status=PreflightStatus.READY, checks=[])

            async def fetch_metadata(self, input: MetadataInput) -> SqlMetadataOutput:
                return SqlMetadataOutput(objects=[])

        handler = _FailingHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        with pytest.raises(RuntimeError, match="boom"):
            await test_auth(AuthInput(credentials=[]))

        assert handler._context is None

    @pytest.mark.asyncio
    async def test_secret_store_pulled_from_infrastructure_context(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        fake_secret_store = mock.MagicMock(name="SecretStore")
        fake_infra = mock.MagicMock()
        fake_infra.secret_store = fake_secret_store

        with mock.patch(
            "application_sdk.execution._temporal.sdr.get_infrastructure",
            return_value=fake_infra,
        ):
            await test_auth(AuthInput(credentials=[]))

        assert handler.context_during_call is not None
        assert (
            handler.context_during_call._secret_store is fake_secret_store  # type: ignore[attr-defined]
        )

    @pytest.mark.asyncio
    async def test_secret_store_is_none_when_no_infrastructure(self) -> None:
        handler = _StubHandler()
        activities = build_sdr_activities(handler, app_name="myapp")
        by_name = {
            getattr(a, "__temporal_activity_definition").name: a for a in activities
        }
        test_auth = by_name[SDR_TEST_AUTH_ACTIVITY]

        with mock.patch(
            "application_sdk.execution._temporal.sdr.get_infrastructure",
            return_value=None,
        ):
            await test_auth(AuthInput(credentials=[]))

        assert handler.context_during_call is not None
        assert handler.context_during_call._secret_store is None  # type: ignore[attr-defined]
