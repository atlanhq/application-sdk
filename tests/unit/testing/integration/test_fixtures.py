"""Unit tests for the shared in-process integration fixture set."""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.integration import _errors, fixtures
from application_sdk.testing.integration.fixtures import (
    CLEANUP_INTERCEPTOR_ENV,
    AppExecutor,
    KitOptions,
)

_APP_NAME = "kit-test-app"


def _fn(fixture: Any) -> Any:
    """The plain function behind a pytest fixture definition."""
    return getattr(fixture, "__wrapped__", fixture)


def _params(fixture: Any) -> list[str]:
    return list(inspect.signature(_fn(fixture)).parameters)


class _FakeApp:
    _app_name = _APP_NAME


@dataclass
class _RecordingBackend:
    calls: list[dict[str, Any]]

    async def execute(self, app_cls: Any, input_data: Any, **kwargs: Any) -> str:
        self.calls.append({"app_cls": app_cls, "input_data": input_data, **kwargs})
        return "executed"


class _FakeRegistry:
    def list_apps(self) -> list[str]:
        return [_APP_NAME]


class TestExecutorShim:
    async def test_entry_point_reaches_the_backend(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(  # type: ignore[arg-type]
            _FakeApp, {"x": 1}, entry_point="extract"
        )
        assert backend.calls[0]["entry_point"] == "extract"

    async def test_entry_point_defaults_to_none(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(_FakeApp, {})  # type: ignore[arg-type]
        assert backend.calls[0]["entry_point"] is None

    async def test_context_named_from_app(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(_FakeApp, {})  # type: ignore[arg-type]
        context = backend.calls[0]["context"]
        assert context.app_name == _APP_NAME
        assert context.run_id == _APP_NAME

    async def test_execution_id_prefix_becomes_run_id(self) -> None:
        backend = _RecordingBackend(calls=[])
        await AppExecutor(backend=backend).execute_app(  # type: ignore[arg-type]
            _FakeApp, {}, execution_id_prefix="run-7"
        )
        assert backend.calls[0]["context"].run_id == "run-7"


class TestPreconditions:
    def test_env_set_after_import_is_a_loud_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "APPLICATION_NAME", "snapshotted")
        monkeypatch.setenv(fixtures.APPLICATION_NAME_ENV, "live")
        with pytest.raises(_errors.IntegrationEnvOrderingError) as exc:
            fixtures._verify_env_ordering()
        assert exc.value.resource == fixtures.APPLICATION_NAME_ENV

    def test_matching_env_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from application_sdk import constants

        monkeypatch.setattr(constants, "APPLICATION_NAME", "same")
        monkeypatch.setenv(fixtures.APPLICATION_NAME_ENV, "same")
        monkeypatch.setattr(constants, "DEPLOYMENT_NAME", "ci")
        monkeypatch.setenv(fixtures.DEPLOYMENT_NAME_ENV, "ci")
        fixtures._verify_env_ordering()

    def test_unset_env_warns_rather_than_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(fixtures.APPLICATION_NAME_ENV, raising=False)
        monkeypatch.delenv(fixtures.DEPLOYMENT_NAME_ENV, raising=False)
        fixtures._verify_env_ordering()

    def test_unregistered_app_fails_before_the_worker_is_built(self) -> None:
        with pytest.raises(_errors.AppRegistrationMissingError):
            fixtures._verify_registration(_FakeApp)  # type: ignore[arg-type]

    def test_registered_app_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from application_sdk.app.registry import AppRegistry

        monkeypatch.setattr(AppRegistry, "get_instance", lambda: _FakeRegistry())
        fixtures._verify_registration(_FakeApp)  # type: ignore[arg-type]

    def test_import_time_check_is_wired(self) -> None:
        source = inspect.getsource(fixtures)
        assert "\n_verify_env_ordering()\n" in source


class TestOverrideFixtures:
    def test_app_cls_must_be_overridden(self) -> None:
        with pytest.raises(_errors.AppRegistrationMissingError) as exc:
            _fn(fixtures.integration_app_cls)()
        assert exc.value.resource == "integration_app_cls"

    def test_task_queue_follows_the_convention(self) -> None:
        assert _fn(fixtures.integration_task_queue)(_FakeApp) == f"{_APP_NAME}-queue"

    def test_source_defaults_to_none(self) -> None:
        assert _fn(fixtures.integration_source)() is None

    def test_secrets_default_empty(self) -> None:
        assert _fn(fixtures.integration_secrets)(None) == {}

    def test_options_default(self) -> None:
        assert _fn(fixtures.integration_options)() == KitOptions()

    def test_star_import_exposes_every_fixture_and_override(self) -> None:
        for name in (
            "integration_app_cls",
            "integration_task_queue",
            "integration_source",
            "integration_secrets",
            "integration_options",
            "store_root",
            "infrastructure",
            "embedded_temporal",
            "temporal_client",
            "worker",
            "executor",
        ):
            assert name in fixtures.__all__


class TestFixtureGraph:
    def test_worker_depends_on_infrastructure(self) -> None:
        assert "infrastructure" in _params(fixtures.worker)

    def test_executor_depends_on_worker(self) -> None:
        assert "worker" in _params(fixtures.executor)

    def test_infrastructure_sees_the_source_and_secrets(self) -> None:
        params = _params(fixtures.infrastructure)
        assert "integration_source" in params
        assert "integration_secrets" in params
        assert "store_root" in params

    def test_client_takes_the_runtime_namespace(self) -> None:
        source = inspect.getsource(_fn(fixtures.temporal_client))
        assert "namespace=embedded_temporal.namespace" in source


class TestArtifactPreservation:
    def test_defaults_the_interceptor_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        fixtures._apply_artifact_preservation(KitOptions())
        assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "false"

    def test_explicit_setting_wins_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings = _capture_warnings(monkeypatch)
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "true")
        fixtures._apply_artifact_preservation(KitOptions())
        assert os.environ[CLEANUP_INTERCEPTOR_ENV] == "true"
        assert len(warnings) == 1
        assert CLEANUP_INTERCEPTOR_ENV in warnings[0]

    def test_explicit_false_is_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        warnings = _capture_warnings(monkeypatch)
        monkeypatch.setenv(CLEANUP_INTERCEPTOR_ENV, "false")
        fixtures._apply_artifact_preservation(KitOptions())
        assert warnings == []

    def test_declining_leaves_the_environment_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CLEANUP_INTERCEPTOR_ENV, raising=False)
        fixtures._apply_artifact_preservation(KitOptions(preserve_artifacts=False))
        assert CLEANUP_INTERCEPTOR_ENV not in os.environ


class TestInfrastructureFixture:
    def test_mocked_stores_installed_then_torn_down(self, tmp_path: Path) -> None:
        from application_sdk.infrastructure.context import get_infrastructure
        from application_sdk.observability.observability import AtlanObservability
        from application_sdk.testing.mocks import MockSecretStore, MockStateStore

        before = AtlanObservability._deployment_store
        gen = _fn(fixtures.infrastructure)(tmp_path, None, {"k": "v"})
        ctx = next(gen)
        assert isinstance(ctx.state_store, MockStateStore)
        assert isinstance(ctx.secret_store, MockSecretStore)
        assert ctx.storage is not None
        assert get_infrastructure() is ctx
        assert AtlanObservability._deployment_store is not before
        with pytest.raises(StopIteration):
            next(gen)
        assert get_infrastructure() is None
        assert AtlanObservability._deployment_store is before

    async def test_secrets_are_seeded(self, tmp_path: Path) -> None:
        gen = _fn(fixtures.infrastructure)(tmp_path, None, {"creds": '{"a": 1}'})
        ctx = next(gen)
        try:
            assert await ctx.secret_store.get("creds") == '{"a": 1}'
        finally:
            with pytest.raises(StopIteration):
                next(gen)


class TestClientFixture:
    async def test_prometheus_off_converter_on_namespace_threaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _capture_client(monkeypatch)
        await _fn(fixtures.temporal_client)(_FakeRuntime(), _FakeApp, KitOptions())
        assert calls[0]["enable_prometheus"] is False
        assert calls[0]["data_converter"] == "converter"
        assert calls[0]["namespace"] == "test-ns"
        assert calls[0]["host"] == "127.0.0.1:7233"

    async def test_prometheus_can_be_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _capture_client(monkeypatch)
        await _fn(fixtures.temporal_client)(
            _FakeRuntime(), _FakeApp, KitOptions(enable_prometheus=True)
        )
        assert calls[0]["enable_prometheus"] is True

    async def test_converter_can_be_declined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _capture_client(monkeypatch)
        await _fn(fixtures.temporal_client)(
            _FakeRuntime(), _FakeApp, KitOptions(data_converter=False)
        )
        assert calls[0]["data_converter"] is None


def _capture_warnings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        fixtures.logger,
        "warning",
        lambda msg, *args, **kwargs: seen.append(msg % args if args else msg),
    )
    return seen


def _capture_client(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from application_sdk import execution

    calls: list[dict[str, Any]] = []

    async def fake_client(**kwargs: Any) -> str:
        calls.append(kwargs)
        return "client"

    monkeypatch.setattr(execution, "create_temporal_client", fake_client)
    monkeypatch.setattr(
        execution, "create_data_converter_for_app", lambda app_cls: "converter"
    )
    return calls


@dataclass
class _FakeRuntime:
    host: str = "127.0.0.1:7233"
    namespace: str = "test-ns"
