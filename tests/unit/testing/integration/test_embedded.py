"""Unit tests for the shared in-process integration fixture set."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.integration import _errors, embedded
from application_sdk.testing.integration.embedded import (
    CLEANUP_INTERCEPTOR_ENV,
    AppExecutor,
    integration_kit,
)

_APP_NAME = "kit-test-app"


def _fn(fixture: Any) -> Any:
    """The plain function behind a pytest fixture definition."""
    return getattr(fixture, "__wrapped__", fixture)


class _FakeApp:
    _app_name = _APP_NAME


@dataclass
class _RecordingBackend:
    calls: list[dict[str, Any]]

    async def execute(self, app_cls: Any, input_data: Any, **kwargs: Any) -> str:
        self.calls.append({"app_cls": app_cls, "input_data": input_data, **kwargs})
        return "executed"


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the registration and env-ordering preconditions pass."""
    monkeypatch.setattr(embedded, "_verify_env_ordering", lambda: None)
    monkeypatch.setattr(embedded, "_verify_registration", lambda app_cls: None)


class TestExecutorShim:
    @pytest.mark.asyncio
    async def test_entry_point_reaches_the_backend(self) -> None:
        backend = _RecordingBackend(calls=[])
        executor = AppExecutor(backend=backend)
        await executor.execute_app(_FakeApp, "input", entry_point="extract-lineage")
        assert backend.calls[0]["entry_point"] == "extract-lineage"

    @pytest.mark.asyncio
    async def test_entry_point_defaults_to_none(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(_FakeApp, "input")
        assert backend.calls[0]["entry_point"] is None

    @pytest.mark.asyncio
    async def test_context_named_from_app(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(_FakeApp, "input")
        context = backend.calls[0]["context"]
        assert context.app_name == _APP_NAME
        assert context.run_id == _APP_NAME

    @pytest.mark.asyncio
    async def test_execution_id_prefix_becomes_run_id(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(
            _FakeApp, "input", execution_id_prefix="scenario-1"
        )
        assert backend.calls[0]["context"].run_id == "scenario-1"


class TestPreconditions:
    def test_env_set_after_import_is_a_loud_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "APPLICATION_NAME", "default")
        monkeypatch.setenv(embedded.APPLICATION_NAME_ENV, "yourapp")
        with pytest.raises(_errors.IntegrationEnvOrderingError) as excinfo:
            embedded._verify_env_ordering()
        assert "setdefault" in str(excinfo.value.suggested_action)

    def test_matching_env_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "APPLICATION_NAME", "yourapp")
        monkeypatch.setattr(constants, "DEPLOYMENT_NAME", "ci")
        monkeypatch.setenv(embedded.APPLICATION_NAME_ENV, "yourapp")
        monkeypatch.setenv(embedded.DEPLOYMENT_NAME_ENV, "ci")
        embedded._verify_env_ordering()

    def test_unset_env_warns_rather_than_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(embedded.APPLICATION_NAME_ENV, raising=False)
        monkeypatch.delenv(embedded.DEPLOYMENT_NAME_ENV, raising=False)
        embedded._verify_env_ordering()

    def test_unregistered_app_fails_before_the_worker_is_built(self) -> None:
        with pytest.raises(_errors.AppRegistrationMissingError) as excinfo:
            embedded._verify_registration(_FakeApp)
        assert "registry" in str(excinfo.value.message)

    def test_registered_app_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from application_sdk.app.registry import AppRegistry

        monkeypatch.setattr(
            AppRegistry, "get_instance", classmethod(lambda cls: _FakeRegistry())
        )
        embedded._verify_registration(_FakeApp)


class _FakeRegistry:
    def list_apps(self) -> list[str]:
        return [_APP_NAME]


class TestKitWiring:
    def test_returns_the_six_fixtures(self, registered: None) -> None:
        kit = integration_kit(app_cls=_FakeApp, task_queue="q")
        for name in (
            "store_root",
            "infrastructure",
            "embedded_temporal",
            "temporal_client",
            "worker",
            "executor",
        ):
            fixture = getattr(kit, name)
            assert hasattr(fixture, "_fixture_function_marker"), name
            assert fixture._fixture_function_marker.scope == "session", name

    def test_worker_depends_on_infrastructure(self, registered: None) -> None:
        kit = integration_kit(app_cls=_FakeApp, task_queue="q")
        assert "infrastructure" in inspect.signature(_fn(kit.worker)).parameters

    def test_executor_depends_on_worker(self, registered: None) -> None:
        kit = integration_kit(app_cls=_FakeApp, task_queue="q")
        assert "worker" in inspect.signature(_fn(kit.executor)).parameters

    def test_cleanup_interceptor_disabled_by_default(
        self, registered: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        integration_kit(app_cls=_FakeApp, task_queue="q")
        import os

        assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "false"

    def test_explicit_cleanup_setting_is_respected(
        self, registered: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "true")
        integration_kit(app_cls=_FakeApp, task_queue="q")
        import os

        assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "true"

    def test_artifact_preservation_can_be_declined(
        self, registered: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        integration_kit(app_cls=_FakeApp, task_queue="q", preserve_artifacts=False)
        import os

        assert CLEANUP_INTERCEPTOR_ENV not in os.environ

    def test_observability_deployment_store_prewired(self, registered: None) -> None:
        from application_sdk.observability.observability import AtlanObservability

        AtlanObservability._deployment_store = None
        integration_kit(app_cls=_FakeApp, task_queue="q")
        assert AtlanObservability._deployment_store is not None


class TestInfrastructureFixture:
    def _build(self, kit_kwargs: dict[str, Any]) -> Any:
        return integration_kit(app_cls=_FakeApp, task_queue="q", **kit_kwargs)

    def test_mocked_infrastructure_is_the_default(
        self, registered: None, tmp_path: Path
    ) -> None:
        from application_sdk.testing.mocks import MockSecretStore, MockStateStore

        kit = self._build({})
        ctx = _fn(kit.infrastructure)(_FakeRequest({}), tmp_path)
        assert isinstance(ctx.state_store, MockStateStore)
        assert isinstance(ctx.secret_store, MockSecretStore)
        assert ctx.storage is not None

    def test_source_fixture_resolved_by_name(
        self, registered: None, tmp_path: Path
    ) -> None:
        request = _FakeRequest({"yourapp_source": {"host": "http://source.invalid"}})
        kit = self._build({"source_fixture": "yourapp_source"})
        _fn(kit.infrastructure)(request, tmp_path)
        assert request.requested == ["yourapp_source"]

    @pytest.mark.asyncio
    async def test_secrets_seeded_from_the_resolved_source(
        self, registered: None, tmp_path: Path
    ) -> None:
        request = _FakeRequest({"src": {"host": "http://source.invalid"}})
        kit = self._build(
            {
                "source_fixture": "src",
                "secrets": lambda source: {"key": source["host"]},
            }
        )
        ctx = _fn(kit.infrastructure)(request, tmp_path)
        assert await ctx.secret_store.get("key") == "http://source.invalid"

    def test_real_infrastructure_is_opt_in(
        self, registered: None, tmp_path: Path
    ) -> None:
        sentinel = object()
        seen: list[Path] = []

        def factory(store_root: Path) -> Any:
            seen.append(store_root)
            return sentinel

        kit = self._build({"infrastructure_factory": factory})
        assert _fn(kit.infrastructure)(_FakeRequest({}), tmp_path) is sentinel
        assert seen == [tmp_path]

    def test_infrastructure_is_installed_globally(
        self, registered: None, tmp_path: Path
    ) -> None:
        from application_sdk.infrastructure.context import get_infrastructure

        kit = self._build({})
        ctx = _fn(kit.infrastructure)(_FakeRequest({}), tmp_path)
        assert get_infrastructure() is ctx


class TestClientFixture:
    @pytest.mark.asyncio
    async def test_prometheus_off_and_converter_on_by_default(
        self, registered: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_client(monkeypatch)
        kit = integration_kit(app_cls=_FakeApp, task_queue="q")
        await _fn(kit.temporal_client)(_FakeRuntime())
        assert calls[0]["enable_prometheus"] is False
        assert calls[0]["data_converter"] == "converter-for-kit-test-app"

    @pytest.mark.asyncio
    async def test_prometheus_can_be_enabled(
        self, registered: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_client(monkeypatch)
        kit = integration_kit(app_cls=_FakeApp, task_queue="q", enable_prometheus=True)
        await _fn(kit.temporal_client)(_FakeRuntime())
        assert calls[0]["enable_prometheus"] is True

    @pytest.mark.asyncio
    async def test_converter_can_be_declined(
        self, registered: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _patch_client(monkeypatch)
        kit = integration_kit(app_cls=_FakeApp, task_queue="q", data_converter=False)
        await _fn(kit.temporal_client)(_FakeRuntime())
        assert calls[0]["data_converter"] is None


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import application_sdk.execution as execution

    calls: list[dict[str, Any]] = []

    async def fake_create_temporal_client(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "client"

    monkeypatch.setattr(
        execution, "create_temporal_client", fake_create_temporal_client
    )
    monkeypatch.setattr(
        execution,
        "create_data_converter_for_app",
        lambda app_cls: f"converter-for-{app_cls._app_name}",
    )
    return calls


@dataclass
class _FakeRuntime:
    host: str = "127.0.0.1:7233"


class _FakeRequest:
    """Minimal stand-in for ``pytest.FixtureRequest``."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values
        self.requested: list[str] = []

    def getfixturevalue(self, name: str) -> Any:
        self.requested.append(name)
        return self._values[name]
