"""Unit tests for the main entry point."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.main import (
    AppConfig,
    _create_infrastructure,
    _derive_service_name,
    _flush_observability,
    _install_excepthook,
    _install_graceful_signal_handlers,
    _log_dapr_components,
    _loop_exception_handler,
    _parse_all_component_yamls,
    main,
    parse_args,
    run_combined_mode,
    run_dev_combined,
    run_handler_mode,
    run_main,
    run_worker_mode,
)


class TestDeriveServiceName:
    """Tests for _derive_service_name()."""

    def test_pascal_case_to_kebab(self) -> None:
        result = _derive_service_name("my_package.apps:MyAppExtractor")
        assert result == "my-app-extractor"

    def test_single_word(self) -> None:
        result = _derive_service_name("pkg:Greeter")
        assert result == "greeter"

    def test_no_colon_returns_fallback(self) -> None:
        result = _derive_service_name("my_package.apps")
        assert result == "application-sdk"

    def test_consecutive_uppercase(self) -> None:
        result = _derive_service_name("pkg:SQLExtractor")
        assert result == "sql-extractor"

    def test_openapi_consecutive_uppercase(self) -> None:
        result = _derive_service_name("app.connector:OpenAPIConnector")
        assert result == "open-api-connector"

    def test_empty_string_returns_fallback(self) -> None:
        result = _derive_service_name("")
        assert result == "application-sdk"


class TestAppConfigFromArgsAndEnv:
    """Tests for AppConfig.from_args_and_env()."""

    def _make_args(self, **kwargs: object) -> argparse.Namespace:
        defaults = {
            "mode": None,
            "app": None,
            "handler": None,
            "temporal_host": None,
            "temporal_namespace": None,
            "task_queue": None,
            "handler_host": None,
            "handler_port": None,
            "log_level": None,
            "service_name": None,
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_mode_from_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        args = self._make_args(mode="worker", app="pkg:App")
        config = AppConfig.from_args_and_env(args)
        assert config.mode == "worker"

    def test_mode_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "handler")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.mode == "handler"

    def test_app_module_from_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_APP_MODULE", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        args = self._make_args(mode="worker", app="my_pkg.apps:MyApp")
        config = AppConfig.from_args_and_env(args)
        assert config.app_module == "my_pkg.apps:MyApp"

    def test_app_module_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "env_pkg:EnvApp")
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.app_module == "env_pkg:EnvApp"

    def test_legacy_application_mode_local_defaults_combined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("APPLICATION_MODE", "LOCAL")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == "combined"

    def test_legacy_application_mode_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("APPLICATION_MODE", "WORKER")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == "worker"

    def test_legacy_application_mode_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("APPLICATION_MODE", "SERVER")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == "handler"

    def test_legacy_application_mode_unset_defaults_combined(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.delenv("APPLICATION_MODE", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        # With neither var set the fallback resolves to "combined"
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == "combined"

    def test_atlan_app_mode_takes_precedence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "handler")
        monkeypatch.setenv("APPLICATION_MODE", "WORKER")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == "handler"

    def test_missing_app_module_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_APP_MODULE", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        args = self._make_args(mode="worker")
        with pytest.raises(ValueError, match="App module is required"):
            AppConfig.from_args_and_env(args)

    def test_app_module_parsed_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.primary:PrimaryApp")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.app_module == "app.primary:PrimaryApp"

    def test_single_app_module_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        monkeypatch.setenv("ATLAN_APP_MODULE", "  app.main:MyApp  ")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.app_module == "app.main:MyApp"

    def test_temporal_host_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "temporal.prod:7233")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.temporal_host == "temporal.prod:7233"

    def test_service_name_derived_when_not_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:MyApp")
        monkeypatch.delenv("ATLAN_SERVICE_NAME", raising=False)
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.service_name == "my-app"

    def test_default_temporal_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_TEMPORAL_HOST", raising=False)
        monkeypatch.delenv("ATLAN_WORKFLOW_HOST", raising=False)
        monkeypatch.delenv("ATLAN_WORKFLOW_PORT", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.temporal_host == "localhost:7233"

    def test_default_handler_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODE", "handler")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_HANDLER_PORT", raising=False)
        monkeypatch.delenv("ATLAN_APP_HTTP_PORT", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.handler_port == 8000

    # --- v2 environment variable fallback tests ---

    def test_v2_temporal_host_and_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_TEMPORAL_HOST", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_HOST", "temporal-internal.svc")
        monkeypatch.setenv("ATLAN_WORKFLOW_PORT", "7236")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.temporal_host == "temporal-internal.svc:7236"

    def test_v2_temporal_host_default_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_TEMPORAL_HOST", raising=False)
        monkeypatch.delenv("ATLAN_WORKFLOW_PORT", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_HOST", "temporal-internal.svc")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.temporal_host == "temporal-internal.svc:7233"

    def test_v3_temporal_host_takes_precedence_over_v2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "v3-host:7233")
        monkeypatch.setenv("ATLAN_WORKFLOW_HOST", "v2-host")
        monkeypatch.setenv("ATLAN_WORKFLOW_PORT", "7236")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.temporal_host == "v3-host:7233"

    def test_v2_temporal_namespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_TEMPORAL_NAMESPACE", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_NAMESPACE", "production")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.temporal_namespace == "production"

    def test_v2_handler_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_HANDLER_HOST", raising=False)
        monkeypatch.setenv("ATLAN_APP_HTTP_HOST", "1.2.3.4")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.handler_host == "1.2.3.4"

    def test_v2_handler_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_HANDLER_PORT", raising=False)
        monkeypatch.setenv("ATLAN_APP_HTTP_PORT", "9000")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.handler_port == 9000

    def test_v2_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_LOG_LEVEL", raising=False)
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.log_level == "DEBUG"

    def test_v3_log_level_takes_precedence_over_v2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setenv("ATLAN_LOG_LEVEL", "WARNING")
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.log_level == "WARNING"

    def test_task_queue_derived_from_app_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:MyConnector")
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_APPLICATION_NAME", raising=False)
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.task_queue == "my-connector-queue"

    def test_task_queue_from_application_and_deployment_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.connector:OpenAPIConnector")
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")
        monkeypatch.setenv("ATLAN_DEPLOYMENT_NAME", "production")
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.task_queue == "atlan-openapi-production"

    def test_task_queue_from_application_name_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.connector:OpenAPIConnector")
        monkeypatch.setenv("ATLAN_APPLICATION_NAME", "openapi")
        monkeypatch.delenv("ATLAN_DEPLOYMENT_NAME", raising=False)
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.task_queue == "openapi"

    def test_task_queue_explicit_overrides_derived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:MyConnector")
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "custom-queue")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.task_queue == "custom-queue"


class TestRunMain:
    """Tests for run_main()."""

    def test_unknown_mode_raises_value_error(self) -> None:
        config = AppConfig(mode="invalid", app_module="pkg:App")
        with pytest.raises(ValueError, match="Unknown mode"):
            run_main(config)

    def test_worker_mode_calls_asyncio_run(self) -> None:
        config = AppConfig(mode="worker", app_module="pkg:App")
        with mock.patch("application_sdk.main.asyncio.run") as mock_run:
            with mock.patch("application_sdk.main.run_worker_mode"):
                mock_run.side_effect = lambda coro: None
                run_main(config)
                mock_run.assert_called_once()

    def test_handler_mode_calls_run_handler_mode(self) -> None:
        config = AppConfig(mode="handler", app_module="pkg:App")
        with mock.patch("application_sdk.main.run_handler_mode") as mock_handler:
            run_main(config)
            mock_handler.assert_called_once_with(config)

    def test_combined_mode_calls_asyncio_run(self) -> None:
        config = AppConfig(mode="combined", app_module="pkg:App")
        with mock.patch("application_sdk.main.asyncio.run") as mock_run:
            with mock.patch("application_sdk.main.run_combined_mode"):
                mock_run.side_effect = lambda coro: None
                run_main(config)
                mock_run.assert_called_once()


class TestParseArgs:
    """Tests for parse_args()."""

    def test_parse_mode_and_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv", ["prog", "--mode", "worker", "--app", "pkg:Cls"]
        )
        args = parse_args()
        assert args.mode == "worker"
        assert args.app == "pkg:Cls"

    def test_parse_short_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog", "-m", "handler", "-a", "pkg:Cls"])
        args = parse_args()
        assert args.mode == "handler"
        assert args.app == "pkg:Cls"

    def test_parse_temporal_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "sys.argv",
            [
                "prog",
                "--mode",
                "worker",
                "--app",
                "pkg:Cls",
                "--temporal-host",
                "temporal.prod:7233",
            ],
        )
        args = parse_args()
        assert args.temporal_host == "temporal.prod:7233"

    def test_defaults_are_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog"])
        args = parse_args()
        assert args.mode is None
        assert args.app is None


class TestLogDaprComponents:
    """Tests for _log_dapr_components()."""

    async def test_returns_registered_component_names(self, tmp_path: Path) -> None:
        """Returns the set of registered component names on success."""
        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "registeredComponents": [
                {"name": "atlan-statestore", "type": "bindings.redis", "version": "v1"},
                {
                    "name": "atlan-secretstore",
                    "type": "bindings.redis",
                    "version": "v1",
                },
            ]
        }

        result = await _log_dapr_components(dapr_client, tmp_path)

        assert result == {"atlan-statestore", "atlan-secretstore"}

    async def test_returns_empty_set_on_metadata_failure(self, tmp_path: Path) -> None:
        """Returns empty set when Dapr metadata query fails."""
        dapr_client = AsyncMock()
        dapr_client.get_metadata.side_effect = Exception("Dapr not reachable")

        result = await _log_dapr_components(dapr_client, tmp_path)

        assert result == set()

    async def test_return_type_is_set_of_str(self, tmp_path: Path) -> None:
        """Return type is set[str] — every element is a string."""
        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "registeredComponents": [
                {"name": "my-component", "type": "bindings.redis", "version": "v1"},
            ]
        }

        result = await _log_dapr_components(dapr_client, tmp_path)

        assert isinstance(result, set)
        assert all(isinstance(name, str) for name in result)


class TestCreateInfrastructureEventBinding:
    """Tests for _create_infrastructure() conditional event_binding."""

    _DAPR_CLIENT_MOD = "application_sdk.infrastructure._dapr.client"
    _STORAGE_MOD = "application_sdk.storage"

    def _make_dapr_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")

    async def test_event_binding_created_when_component_registered(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DaprBinding is created when the eventstore component is in the registered set."""
        from application_sdk.constants import EVENT_STORE_NAME

        self._make_dapr_env(monkeypatch)

        with (
            patch(
                "application_sdk.main._log_dapr_components",
                new_callable=AsyncMock,
                return_value={EVENT_STORE_NAME},
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(f"{self._STORAGE_MOD}.create_store_from_binding"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprBinding") as mock_binding,
        ):
            infra = await _create_infrastructure()

        mock_binding.assert_called_once()
        assert infra.event_binding is not None

    async def test_event_binding_is_none_when_component_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """event_binding is None when eventstore is not registered."""
        self._make_dapr_env(monkeypatch)

        with (
            patch(
                "application_sdk.main._log_dapr_components",
                new_callable=AsyncMock,
                return_value=set(),
            ),
            patch(f"{self._DAPR_CLIENT_MOD}.AsyncDaprClient"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprStateStore"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprSecretStore"),
            patch(f"{self._STORAGE_MOD}.create_store_from_binding"),
            patch(f"{self._DAPR_CLIENT_MOD}.DaprBinding") as mock_binding,
        ):
            infra = await _create_infrastructure()

        mock_binding.assert_not_called()
        assert infra.event_binding is None


class TestInstallGracefulSignalHandlers:
    """Tests for _install_graceful_signal_handlers()."""

    def test_registers_sigint_and_sigterm(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            registered: list[signal.Signals] = []

            def _fake_handler(sig: signal.Signals, cb: object) -> None:
                registered.append(sig)

            loop.add_signal_handler = _fake_handler  # type: ignore[method-assign]
            _install_graceful_signal_handlers(loop, lambda: None)
            assert signal.SIGINT in registered
            assert signal.SIGTERM in registered
        finally:
            loop.close()

    def test_windows_fallback_logs_warning_and_does_not_raise(self) -> None:
        loop = asyncio.new_event_loop()
        try:

            def _not_supported(*_: object, **__: object) -> None:
                raise NotImplementedError

            loop.add_signal_handler = _not_supported  # type: ignore[method-assign]
            with patch("application_sdk.main.logger") as mock_log:
                _install_graceful_signal_handlers(loop, lambda: None)

            assert mock_log.warning.call_count == 2
            # sig.name is passed as the %-format arg (index 1 in positional args)
            called_names = {c.args[1] for c in mock_log.warning.call_args_list}
            assert "SIGINT" in called_names
            assert "SIGTERM" in called_names
        finally:
            loop.close()


# --------------------------------------------------------------------------- #
# Helpers for async-mode tests                                                #
# --------------------------------------------------------------------------- #


def _make_async_cm() -> AsyncMock:
    """Return an AsyncMock that works as an `async with` context manager."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _fake_app_class() -> mock.MagicMock:
    """Build a fake App class with attributes the run modes read."""
    cls = mock.MagicMock()
    cls._app_name = "fake-app"
    cls._app_version = "1.0.0"
    cls.__name__ = "FakeApp"
    cls.__module__ = "fake_pkg.fake_app"
    return cls


# --------------------------------------------------------------------------- #
# AppConfig — additional edge cases                                           #
# --------------------------------------------------------------------------- #


class TestAppConfigCommaRejection:
    """AppConfig rejects comma-separated app modules (multi-app v2 pattern)."""

    def test_comma_in_app_module_raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ATLAN_APP_MODULE with a comma must be rejected with a clear error."""
        monkeypatch.setenv("ATLAN_APP_MODE", "worker")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:AppA,pkg:AppB")
        args = argparse.Namespace(
            mode=None,
            app=None,
            handler=None,
            temporal_host=None,
            temporal_namespace=None,
            task_queue=None,
            handler_host=None,
            handler_port=None,
            log_level=None,
            service_name=None,
        )
        with pytest.raises(ValueError, match="comma"):
            AppConfig.from_args_and_env(args)

    def test_post_init_derives_task_queue(self) -> None:
        """__post_init__ derives task_queue from app_module when missing."""
        cfg = AppConfig(mode="worker", app_module="pkg:MyApp")
        assert cfg.task_queue == "my-app-queue"


# --------------------------------------------------------------------------- #
# _create_infrastructure — Dapr-not-set path                                  #
# --------------------------------------------------------------------------- #


class TestCreateInfrastructureNoDapr:
    """_create_infrastructure() raises when Dapr sidecar env var is absent."""

    async def test_raises_runtime_error_when_no_dapr_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without DAPR_HTTP_PORT, _create_infrastructure must raise RuntimeError."""
        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)
        with pytest.raises(RuntimeError, match="Dapr sidecar not detected"):
            await _create_infrastructure()


# --------------------------------------------------------------------------- #
# _parse_all_component_yamls                                                  #
# --------------------------------------------------------------------------- #


class TestParseAllComponentYamls:
    """Tests for _parse_all_component_yamls()."""

    def test_returns_safe_metadata_only(self, tmp_path: Path) -> None:
        """Allowlisted keys are returned, secret-y keys are filtered out."""
        (tmp_path / "comp.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: my-store\n"
            "spec:\n"
            "  type: state.redis\n"
            "  metadata:\n"
            "    - name: bucket\n"
            "      value: my-bucket\n"
            "    - name: accessKey\n"
            "      value: SECRET\n"
        )
        result = _parse_all_component_yamls(tmp_path)
        assert "my-store" in result
        assert result["my-store"].get("bucket") == "my-bucket"
        # accessKey is NOT in the allowlist — must be excluded
        assert "accessKey" not in result["my-store"]

    def test_skips_non_component_kinds(self, tmp_path: Path) -> None:
        """Non-Component YAMLs (e.g. Configuration) are ignored."""
        (tmp_path / "cfg.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Configuration\n"
            "metadata:\n"
            "  name: my-cfg\n"
        )
        assert _parse_all_component_yamls(tmp_path) == {}

    def test_skips_components_without_name(self, tmp_path: Path) -> None:
        """Components missing metadata.name are skipped (no KeyError)."""
        (tmp_path / "noname.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Component\n"
            "metadata: {}\n"
            "spec:\n"
            "  type: state.redis\n"
        )
        assert _parse_all_component_yamls(tmp_path) == {}

    def test_returns_empty_dict_on_parse_error(self, tmp_path: Path) -> None:
        """Malformed YAML must not raise — returns empty dict."""
        (tmp_path / "bad.yaml").write_text("not: yaml: : :\nlol")
        with patch("application_sdk.main.logger") as mock_log:
            result = _parse_all_component_yamls(tmp_path)
        assert result == {}
        # Failure must be observable as a warning (not silently swallowed).
        assert mock_log.warning.called


# --------------------------------------------------------------------------- #
# _flush_observability                                                        #
# --------------------------------------------------------------------------- #


class TestFlushObservability:
    """Tests for _flush_observability()."""

    async def test_calls_atlan_observability_flush_all(self) -> None:
        """Happy path: AtlanObservability.flush_all() is awaited."""
        with patch(
            "application_sdk.observability.observability.AtlanObservability.flush_all",
            new_callable=AsyncMock,
        ) as mock_flush:
            await _flush_observability()
        mock_flush.assert_awaited_once()

    async def test_swallows_exception_and_logs_warning(self) -> None:
        """If flush_all raises, the exception is swallowed and logged."""
        with (
            patch(
                "application_sdk.observability.observability.AtlanObservability.flush_all",
                new_callable=AsyncMock,
                side_effect=RuntimeError("flush failed"),
            ),
            patch("application_sdk.main.logger") as mock_log,
        ):
            await _flush_observability()  # must not raise
        assert mock_log.warning.called


# --------------------------------------------------------------------------- #
# _loop_exception_handler                                                     #
# --------------------------------------------------------------------------- #


class TestLoopExceptionHandler:
    """Tests for _loop_exception_handler()."""

    def test_logs_with_exc_info_when_exception_present(self) -> None:
        """Context with 'exception' is logged with exc_info."""
        loop = MagicMock()
        loop.create_task = MagicMock()
        loop.default_exception_handler = MagicMock()
        exc = RuntimeError("boom")
        ctx = {"exception": exc, "message": "task crashed"}
        with patch("application_sdk.main.logger") as mock_log:
            _loop_exception_handler(loop, ctx)
        assert mock_log.error.called
        # Must call default handler so behavior is not silently dropped
        loop.default_exception_handler.assert_called_once_with(ctx)
        loop.create_task.assert_called_once()

    def test_logs_without_exc_info_when_exception_absent(self) -> None:
        """Context without 'exception' still logs and schedules flush."""
        loop = MagicMock()
        ctx = {"message": "Something happened"}
        with patch("application_sdk.main.logger") as mock_log:
            _loop_exception_handler(loop, ctx)
        assert mock_log.error.called
        loop.create_task.assert_called_once()
        loop.default_exception_handler.assert_called_once_with(ctx)


# --------------------------------------------------------------------------- #
# _install_excepthook                                                         #
# --------------------------------------------------------------------------- #


class TestInstallExcepthook:
    """Tests for _install_excepthook()."""

    def test_installs_hook_that_logs_and_calls_original(self) -> None:
        """The installed hook logs + calls the original sys.excepthook."""
        original_hook = MagicMock()
        with patch.object(sys, "excepthook", original_hook):
            _install_excepthook()
            installed = sys.excepthook
            assert installed is not original_hook
            with (
                patch("application_sdk.main.logger") as mock_log,
                patch("application_sdk.main.asyncio.run") as mock_async_run,
            ):
                exc_type = RuntimeError
                exc_value = RuntimeError("bad")
                installed(exc_type, exc_value, None)
            assert mock_log.error.called
            mock_async_run.assert_called_once()
            original_hook.assert_called_once_with(exc_type, exc_value, None)

    def test_hook_swallows_flush_failure(self) -> None:
        """If asyncio.run fails inside the hook, the original hook still fires."""
        original_hook = MagicMock()
        with patch.object(sys, "excepthook", original_hook):
            _install_excepthook()
            installed = sys.excepthook
            with (
                patch("application_sdk.main.logger"),
                patch(
                    "application_sdk.main.asyncio.run",
                    side_effect=RuntimeError("loop already running"),
                ),
            ):
                installed(RuntimeError, RuntimeError("x"), None)
            # The original hook must still execute — we never mask the crash.
            original_hook.assert_called_once()


# --------------------------------------------------------------------------- #
# main() entry point — exit codes                                             #
# --------------------------------------------------------------------------- #


class TestMainEntryPoint:
    """Tests for the main() CLI entry point."""

    def _patch_run(self, **kwargs: object) -> mock.MagicMock:
        return mock.MagicMock(**kwargs)

    def test_value_error_in_config_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bad config (e.g. missing app module) -> sys.exit(1)."""
        monkeypatch.delenv("ATLAN_APP_MODULE", raising=False)
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        with patch("application_sdk.main._install_excepthook"):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1

    def test_discovery_error_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DiscoveryError raised by run_main propagates as exit(1)."""
        from application_sdk.discovery import DiscoveryError

        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        with (
            patch("application_sdk.main._install_excepthook"),
            patch(
                "application_sdk.main.run_main",
                side_effect=DiscoveryError("not found"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 1

    def test_keyboard_interrupt_exits_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """KeyboardInterrupt is treated as graceful (exit 0)."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        with (
            patch("application_sdk.main._install_excepthook"),
            patch(
                "application_sdk.main.run_main",
                side_effect=KeyboardInterrupt(),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 0

    def test_unhandled_exception_flushes_and_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected Exception triggers flush attempt + exit(1)."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        with (
            patch("application_sdk.main._install_excepthook"),
            patch(
                "application_sdk.main.run_main",
                side_effect=RuntimeError("kaboom"),
            ),
            patch("application_sdk.main.asyncio.run") as mock_async_run,
        ):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 1
        # Best-effort flush must be attempted
        mock_async_run.assert_called()

    def test_success_path_exits_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Successful run_main returns -> exit(0)."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        with (
            patch("application_sdk.main._install_excepthook"),
            patch("application_sdk.main.run_main"),
            pytest.raises(SystemExit) as exc,
        ):
            main()
        assert exc.value.code == 0

    def test_main_installs_excepthook_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_install_excepthook must run before parse_args/run_main."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        order: list[str] = []
        with (
            patch(
                "application_sdk.main._install_excepthook",
                side_effect=lambda: order.append("hook"),
            ),
            patch(
                "application_sdk.main.run_main",
                side_effect=lambda *_: order.append("run"),
            ),
            pytest.raises(SystemExit),
        ):
            main()
        assert order == ["hook", "run"]


# --------------------------------------------------------------------------- #
# run_worker_mode                                                             #
# --------------------------------------------------------------------------- #


class TestRunWorkerMode:
    """Behavioral tests for run_worker_mode()."""

    @pytest.fixture
    def worker_patches(self):
        """Patch the worker-mode collaborators."""
        infra = MagicMock()
        infra.secret_store = MagicMock()
        infra.storage = MagicMock()

        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=infra,
            ),
            patch(
                "application_sdk.main.load_app_class",
                return_value=_fake_app_class(),
            ),
            patch("application_sdk.main.validate_app_class"),
            patch(
                "application_sdk.execution._temporal.backend.create_temporal_client",
                new_callable=AsyncMock,
            ) as mock_create_client,
            patch(
                "application_sdk.execution._temporal.converter.create_data_converter_for_app"
            ),
            patch(
                "application_sdk.execution._temporal.worker.create_worker",
                return_value=_make_async_cm(),
            ),
            patch(
                "application_sdk.infrastructure.context.set_infrastructure"
            ) as mock_set_infra,
            patch(
                "application_sdk.infrastructure.context.close_infrastructure",
                new_callable=AsyncMock,
            ) as mock_close_infra,
            patch(
                "application_sdk.server.health.WorkerHealthServer",
                return_value=_make_async_cm(),
            ) as mock_health,
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
            patch("application_sdk.main._install_graceful_signal_handlers"),
        ):
            yield {
                "infra": infra,
                "create_client": mock_create_client,
                "set_infra": mock_set_infra,
                "close_infra": mock_close_infra,
                "health": mock_health,
            }

    @pytest.fixture
    def auth_manager(self):
        auth_mgr = MagicMock()
        auth_mgr.acquire_initial_token = AsyncMock(return_value="api-key-xyz")
        auth_mgr.start_background_refresh = MagicMock()
        auth_mgr.shutdown = AsyncMock()
        return auth_mgr

    @pytest.fixture
    def wait_patch(self):
        with patch.object(asyncio.Event, "wait", new=AsyncMock(return_value=None)):
            yield

    async def test_runs_through_to_completion(self, worker_patches) -> None:
        """Worker mode wires infra, loads app, creates client, awaits shutdown."""
        cfg = AppConfig(mode="worker", app_module="pkg:FakeApp")

        async def _run_and_signal() -> None:
            # Schedule a shutdown so shutdown_event.wait() returns
            await asyncio.sleep(0)  # let run_worker_mode reach shutdown_event.wait
            # Find the event and set it via the captured signal handler isn't easy;
            # instead, patch shutdown_event by short-circuiting asyncio.Event.wait.

        with patch.object(asyncio.Event, "wait", new=AsyncMock(return_value=None)):
            await run_worker_mode(cfg)

        worker_patches["set_infra"].assert_called_once()
        worker_patches["create_client"].assert_awaited_once()
        worker_patches["close_infra"].assert_awaited_once()
        worker_patches["health"].assert_called_once()

    async def test_auth_enabled_acquires_token(
        self, worker_patches, auth_manager, wait_patch
    ) -> None:
        """When auth_enabled=True, TemporalAuthManager is used and shutdown is awaited."""
        cfg = AppConfig(
            mode="worker",
            app_module="pkg:FakeApp",
            auth_enabled=True,
            auth_client_id="cid",
            auth_client_secret="csec",
            auth_token_url="https://example.com/token",
            auth_base_url="https://example.com",
        )
        with (
            patch(
                "application_sdk.execution._temporal.auth.TemporalAuthManager",
                return_value=auth_manager,
            ),
            patch("application_sdk.execution._temporal.auth.TemporalAuthConfig"),
        ):
            await run_worker_mode(cfg)
        auth_manager.acquire_initial_token.assert_awaited_once()
        auth_manager.start_background_refresh.assert_called_once()
        auth_manager.shutdown.assert_awaited_once()

    async def test_failure_in_create_client_propagates(self, worker_patches) -> None:
        """If create_temporal_client raises, the exception bubbles up (not swallowed)."""
        worker_patches["create_client"].side_effect = RuntimeError("temporal down")
        cfg = AppConfig(mode="worker", app_module="pkg:FakeApp")
        with pytest.raises(RuntimeError, match="temporal down"):
            await run_worker_mode(cfg)


# --------------------------------------------------------------------------- #
# run_handler_mode                                                            #
# --------------------------------------------------------------------------- #


class TestRunHandlerMode:
    """Behavioral tests for run_handler_mode()."""

    def _common_patches(self, infra: MagicMock):
        return (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=infra,
            ),
            patch(
                "application_sdk.main.load_app_class",
                return_value=_fake_app_class(),
            ),
            patch(
                "application_sdk.execution._temporal.converter.create_data_converter_for_app"
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            # These tests use a MagicMock for App, so stub the validator.
            patch("application_sdk.main.validate_app_class"),
        )

    def _make_infra(self) -> MagicMock:
        infra = MagicMock()
        infra.secret_store = MagicMock()
        infra.storage = MagicMock()
        return infra

    def test_uses_default_handler_when_no_custom_loaded(self) -> None:
        """When load_handler_class returns None, DefaultHandler is instantiated."""
        infra = self._make_infra()
        p_infra, p_load_app, p_conv, p_set, p_validate = self._common_patches(infra)

        # asyncio.run must execute coroutines (so _create_infrastructure returns infra)
        # but we don't want _flush_observability's coroutine to run real code.
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_validate,
            patch(
                "application_sdk.main.load_handler_class",
                return_value=None,
            ),
            patch(
                "application_sdk.handler.DefaultHandler",
                return_value=MagicMock(),
            ) as mock_default,
            patch("application_sdk.handler.run_app_handler_service") as mock_run_svc,
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
        ):
            cfg = AppConfig(mode="handler", app_module="pkg:FakeApp")
            run_handler_mode(cfg)

        mock_default.assert_called_once()
        mock_run_svc.assert_called_once()

    def test_uses_custom_handler_when_loaded(self) -> None:
        """When load_handler_class returns a class, that class is instantiated."""
        infra = self._make_infra()
        p_infra, p_load_app, p_conv, p_set, p_validate = self._common_patches(infra)
        custom_cls = MagicMock()
        custom_cls.__name__ = "MyCustomHandler"
        custom_cls.return_value = MagicMock()

        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_validate,
            patch(
                "application_sdk.main.load_handler_class",
                return_value=custom_cls,
            ),
            patch("application_sdk.handler.run_app_handler_service"),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
        ):
            cfg = AppConfig(mode="handler", app_module="pkg:FakeApp")
            run_handler_mode(cfg)

        custom_cls.assert_called_once()

    def test_handler_mode_validates_app_class(self) -> None:
        """run_handler_mode must validate the loaded app class — currently it does not."""
        infra = self._make_infra()
        p_infra, p_load_app, p_conv, p_set, p_validate = self._common_patches(infra)
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_validate,
            patch("application_sdk.main.validate_app_class") as mock_validate,
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch("application_sdk.handler.DefaultHandler"),
            patch("application_sdk.handler.run_app_handler_service"),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
        ):
            cfg = AppConfig(mode="handler", app_module="pkg:FakeApp")
            run_handler_mode(cfg)
        mock_validate.assert_called_once()

    def test_handler_mode_closes_infrastructure(self) -> None:
        """Handler mode should close infrastructure on exit (parity with worker/combined)."""
        infra = self._make_infra()
        p_infra, p_load_app, p_conv, p_set, p_validate = self._common_patches(infra)
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_validate,
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch("application_sdk.handler.DefaultHandler"),
            patch("application_sdk.handler.run_app_handler_service"),
            patch(
                "application_sdk.infrastructure.context.close_infrastructure",
                new_callable=AsyncMock,
            ) as mock_close,
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
        ):
            cfg = AppConfig(mode="handler", app_module="pkg:FakeApp")
            run_handler_mode(cfg)
        mock_close.assert_awaited_once()


# --------------------------------------------------------------------------- #
# run_combined_mode                                                           #
# --------------------------------------------------------------------------- #


class TestRunCombinedMode:
    """Behavioral tests for run_combined_mode()."""

    @pytest.fixture
    def combined_patches(self):
        infra = MagicMock()
        infra.secret_store = MagicMock()
        infra.storage = MagicMock()

        # uvicorn: serve() is awaitable and returns immediately
        uvicorn_server = MagicMock()
        uvicorn_server.serve = AsyncMock(return_value=None)

        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=infra,
            ),
            patch(
                "application_sdk.main.load_app_class",
                return_value=_fake_app_class(),
            ),
            patch("application_sdk.main.validate_app_class"),
            patch(
                "application_sdk.main.load_handler_class",
                return_value=None,
            ),
            patch(
                "application_sdk.execution._temporal.backend.create_temporal_client",
                new_callable=AsyncMock,
            ) as mock_create_client,
            patch(
                "application_sdk.execution._temporal.converter.create_data_converter_for_app"
            ),
            patch(
                "application_sdk.execution._temporal.worker.create_worker",
                return_value=_make_async_cm(),
            ),
            patch("application_sdk.handler.DefaultHandler"),
            patch(
                "application_sdk.handler.create_app_handler_service"
            ) as mock_create_svc,
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            patch(
                "application_sdk.infrastructure.context.close_infrastructure",
                new_callable=AsyncMock,
            ) as mock_close_infra,
            patch(
                "application_sdk.server.health.WorkerHealthServer",
                return_value=_make_async_cm(),
            ),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
            patch("application_sdk.main._install_graceful_signal_handlers"),
            patch("uvicorn.Server", return_value=uvicorn_server),
            patch("uvicorn.Config"),
            patch.object(asyncio.Event, "wait", new=AsyncMock(return_value=None)),
        ):
            yield {
                "infra": infra,
                "create_client": mock_create_client,
                "create_svc": mock_create_svc,
                "close_infra": mock_close_infra,
                "uvicorn_server": uvicorn_server,
            }

    async def test_combined_runs_to_completion(self, combined_patches) -> None:
        """Combined mode creates client, FastAPI app, runs uvicorn, closes infra."""
        cfg = AppConfig(mode="combined", app_module="pkg:FakeApp")
        await run_combined_mode(cfg)
        combined_patches["create_client"].assert_awaited_once()
        combined_patches["create_svc"].assert_called_once()
        combined_patches["uvicorn_server"].serve.assert_awaited()
        combined_patches["close_infra"].assert_awaited_once()

    async def test_reuses_existing_infrastructure(self, combined_patches) -> None:
        """If infra is already set, _create_infrastructure must NOT be called."""
        existing = MagicMock()
        existing.secret_store = MagicMock()
        existing.storage = MagicMock()
        cfg = AppConfig(mode="combined", app_module="pkg:FakeApp")
        with (
            patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=existing,
            ),
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
            ) as no_create,
        ):
            await run_combined_mode(cfg)
        no_create.assert_not_awaited()


# --------------------------------------------------------------------------- #
# run_dev_combined                                                            #
# --------------------------------------------------------------------------- #


class TestRunDevCombined:
    """Behavioral tests for run_dev_combined()."""

    async def test_sets_default_dapr_ports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defaults DAPR_HTTP_PORT/DAPR_GRPC_PORT for dev convenience."""
        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)
        monkeypatch.delenv("DAPR_GRPC_PORT", raising=False)
        app_class = _fake_app_class()
        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ) as mock_run_combined,
        ):
            await run_dev_combined(app_class, host="127.0.0.1", port=9999)
        import os

        assert os.environ.get("DAPR_HTTP_PORT") == "3500"
        assert os.environ.get("DAPR_GRPC_PORT") == "50001"
        mock_run_combined.assert_awaited_once()
        # config passed must reflect dev settings
        cfg = mock_run_combined.await_args.args[0]
        assert cfg.mode == "combined"
        assert cfg.handler_host == "127.0.0.1"
        assert cfg.handler_port == 9999
        # Dev should not start prometheus by default (port collision)
        assert cfg.enable_temporal_core_metrics is False
        # Dev uses ephemeral health port to avoid collision
        assert cfg.health_port == 0

    async def test_credentials_path_schedules_provision_task(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When credentials are provided, a background provisioning task is scheduled."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        captured: list[object] = []

        def _capture_task(coro):
            captured.append(coro)
            # Close it so we don't get a "never awaited" warning
            coro.close()
            return MagicMock()

        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            patch("asyncio.create_task", side_effect=_capture_task),
        ):
            await run_dev_combined(
                _fake_app_class(),
                credentials={"host": "h", "port": "5432"},
                example_input={"connection": {"connection_name": "c"}},
            )

        assert len(captured) == 1, "credentials path must schedule provisioning task"

    async def test_no_credentials_prints_dev_banner(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """When no credentials, a banner with usage hints is printed to stdout."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
        ):
            await run_dev_combined(_fake_app_class(), host="127.0.0.1", port=8000)
        out = capsys.readouterr().out
        assert "Dev server running" in out
        assert "/workflows/v1/start" in out

    async def test_example_input_is_rendered_in_banner(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """example_input shows up as JSON in the banner curl example."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
        ):
            await run_dev_combined(
                _fake_app_class(),
                example_input={"connection": {"connection_name": "abc"}},
            )
        out = capsys.readouterr().out
        assert "abc" in out


# --------------------------------------------------------------------------- #
# Inline-import safety net                                                    #
# --------------------------------------------------------------------------- #


class TestInlineImportSymbols:
    """Verify that symbols imported lazily inside main.py still exist."""

    @pytest.mark.parametrize(
        "module_path,symbol_name",
        [
            ("application_sdk.execution._temporal.backend", "create_temporal_client"),
            (
                "application_sdk.execution._temporal.converter",
                "create_data_converter_for_app",
            ),
            ("application_sdk.execution._temporal.worker", "create_worker"),
            ("application_sdk.execution._temporal.auth", "TemporalAuthConfig"),
            ("application_sdk.execution._temporal.auth", "TemporalAuthManager"),
            ("application_sdk.handler", "DefaultHandler"),
            ("application_sdk.handler", "run_app_handler_service"),
            ("application_sdk.handler", "create_app_handler_service"),
            ("application_sdk.infrastructure.context", "InfrastructureContext"),
            ("application_sdk.infrastructure.context", "set_infrastructure"),
            ("application_sdk.infrastructure.context", "get_infrastructure"),
            ("application_sdk.infrastructure.context", "close_infrastructure"),
            ("application_sdk.infrastructure._dapr.client", "DaprBinding"),
            ("application_sdk.infrastructure._dapr.client", "DaprSecretStore"),
            ("application_sdk.infrastructure._dapr.client", "DaprStateStore"),
            ("application_sdk.infrastructure._dapr.http", "AsyncDaprClient"),
            ("application_sdk.infrastructure._dapr.http", "wait_for_dapr_sidecar"),
            ("application_sdk.app.registry", "AppRegistry"),
            ("application_sdk.app.registry", "TaskRegistry"),
            ("application_sdk.app.base", "_pascal_to_kebab"),
            ("application_sdk.observability.observability", "AtlanObservability"),
            ("application_sdk.server.health", "WorkerHealthServer"),
            ("application_sdk.storage", "create_store_from_binding"),
            ("application_sdk.storage.binding", "_parse_dapr_metadata"),
            ("application_sdk.constants", "DEPLOYMENT_OBJECT_STORE_NAME"),
            ("application_sdk.constants", "EVENT_STORE_NAME"),
            ("application_sdk.constants", "SECRET_STORE_NAME"),
            ("application_sdk.constants", "STATE_STORE_NAME"),
        ],
    )
    def test_inline_import_target_exists(
        self, module_path: str, symbol_name: str
    ) -> None:
        """Every lazy-imported symbol in main.py must exist at its source module."""
        import importlib

        mod = importlib.import_module(module_path)
        assert hasattr(mod, symbol_name), (
            f"main.py imports {symbol_name!r} from {module_path!r} at runtime, "
            "but the symbol no longer exists. Likely renamed/removed upstream."
        )

    def test_atlan_observability_has_flush_all(self) -> None:
        """AtlanObservability.flush_all is awaited from _flush_observability."""
        from application_sdk.observability.observability import AtlanObservability

        assert hasattr(AtlanObservability, "flush_all")


# --------------------------------------------------------------------------- #
# _log_dapr_components — branch with safe_meta                                #
# --------------------------------------------------------------------------- #


class TestLogDaprComponentsSafeMetaBranch:
    """Cover the log path that includes safe metadata details."""

    async def test_logs_with_safe_meta_when_yaml_present(self, tmp_path: Path) -> None:
        """When a component has allowlisted metadata in YAML, it appears in the log line."""
        # Write a component YAML with bucket=my-bucket so the safe-meta path triggers
        (tmp_path / "store.yaml").write_text(
            "apiVersion: dapr.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: my-store\n"
            "spec:\n"
            "  type: state.s3\n"
            "  metadata:\n"
            "    - name: bucket\n"
            "      value: my-bucket\n"
        )
        dapr_client = AsyncMock()
        dapr_client.get_metadata.return_value = {
            "components": [{"name": "my-store", "type": "state.s3", "version": "v1"}]
        }
        with patch("application_sdk.main.logger") as mock_log:
            result = await _log_dapr_components(dapr_client, tmp_path)
        assert result == {"my-store"}
        # The "with safe_meta" code path produces an INFO call whose format
        # string contains a 4th positional placeholder for the detail string.
        info_calls = [c for c in mock_log.info.call_args_list]
        # At least one info should reference the bucket value
        assert any("bucket=my-bucket" in str(c) for c in info_calls)


# --------------------------------------------------------------------------- #
# run_dev_combined — _provision_and_start inner-function                      #
# --------------------------------------------------------------------------- #


class TestRunDevCombinedProvisioning:
    """Exercise the inner _provision_and_start coroutine in the credentials path."""

    async def test_provision_and_start_posts_creds_and_workflow(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The captured provisioning coroutine POSTs to local-vault then /start."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")

        captured: list[object] = []

        def _capture_task(coro):
            captured.append(coro)
            return MagicMock()

        # Build a fake httpx.AsyncClient that records calls
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None

        # Health check returns 200 immediately
        health_resp = MagicMock(status_code=200)
        fake_client.get = AsyncMock(return_value=health_resp)

        # local-vault POST
        cred_resp = MagicMock()
        cred_resp.json = MagicMock(
            return_value={"data": {"credential_guid": "cred-123"}}
        )
        # workflow start POST
        wf_resp = MagicMock()
        wf_resp.json = MagicMock(
            return_value={"data": {"workflow_id": "wf-1", "run_id": "run-1"}}
        )
        fake_client.post = AsyncMock(side_effect=[cred_resp, wf_resp])

        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            patch("asyncio.create_task", side_effect=_capture_task),
            patch("httpx.AsyncClient", return_value=fake_client),
            patch("application_sdk.main.logger"),
        ):
            await run_dev_combined(
                _fake_app_class(),
                credentials={"host": "h"},
                example_input={"connection": {"connection_name": "c"}},
                host="127.0.0.1",
                port=8765,
            )

            # The captured coroutine is _provision_and_start — drive it inside the patches
            assert len(captured) == 1
            await captured[0]

        # Cred POST and workflow POST must both have happened
        post_urls = [c.args[0] for c in fake_client.post.call_args_list if c.args]
        assert any("local-vault" in u for u in post_urls)
        assert any("/workflows/v1/start" in u for u in post_urls)

        # The credential_guid from /local-vault must be injected into the workflow body
        wf_body = fake_client.post.call_args_list[1].kwargs["json"]
        assert wf_body["credential_guid"] == "cred-123"
        assert wf_body["connection"]["connection_name"] == "c"

    async def test_provision_health_poll_swallowed_continues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Health-check failures are swallowed so provisioning still proceeds."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")

        captured: list[object] = []

        def _capture_task(coro):
            captured.append(coro)
            return MagicMock()

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value = fake_client
        fake_client.__aexit__.return_value = None
        # Health check raises every time — provisioning must still proceed
        # (after the loop exits) and POST creds + start workflow.
        fake_client.get = AsyncMock(side_effect=Exception("connection refused"))
        cred_resp = MagicMock()
        cred_resp.json = MagicMock(return_value={"credential_guid": "g1"})
        wf_resp = MagicMock()
        wf_resp.json = MagicMock(
            return_value={"data": {"workflow_id": "w", "run_id": "r"}}
        )
        fake_client.post = AsyncMock(side_effect=[cred_resp, wf_resp])

        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=MagicMock(),
            ),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.main.run_combined_mode",
                new_callable=AsyncMock,
            ),
            patch("asyncio.create_task", side_effect=_capture_task),
            patch("httpx.AsyncClient", return_value=fake_client),
            # Make asyncio.sleep instant
            patch("application_sdk.main.asyncio.sleep", new_callable=AsyncMock),
            patch("application_sdk.main.logger"),
        ):
            await run_dev_combined(
                _fake_app_class(),
                credentials={"host": "h"},
            )
            assert len(captured) == 1
            await captured[0]

        assert fake_client.post.await_count == 2


# --------------------------------------------------------------------------- #
# run_combined_mode — auth path                                               #
# --------------------------------------------------------------------------- #


class TestRunCombinedAuth:
    """Combined mode auth-enabled path."""

    async def test_auth_enabled_acquires_token_and_shuts_down(self) -> None:
        """When auth_enabled, TemporalAuthManager is acquired + shutdown."""
        infra = MagicMock()
        infra.secret_store = MagicMock()
        infra.storage = MagicMock()

        uvicorn_server = MagicMock()
        uvicorn_server.serve = AsyncMock(return_value=None)

        auth_mgr = MagicMock()
        auth_mgr.acquire_initial_token = AsyncMock(return_value="api-key")
        auth_mgr.start_background_refresh = MagicMock()
        auth_mgr.shutdown = AsyncMock()

        cfg = AppConfig(
            mode="combined",
            app_module="pkg:FakeApp",
            auth_enabled=True,
            auth_client_id="cid",
            auth_client_secret="csec",
            auth_token_url="https://x/token",
            auth_base_url="https://x",
        )

        with (
            patch(
                "application_sdk.main._create_infrastructure",
                new_callable=AsyncMock,
                return_value=infra,
            ),
            patch(
                "application_sdk.main.load_app_class",
                return_value=_fake_app_class(),
            ),
            patch("application_sdk.main.validate_app_class"),
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch(
                "application_sdk.execution._temporal.backend.create_temporal_client",
                new_callable=AsyncMock,
            ),
            patch(
                "application_sdk.execution._temporal.converter.create_data_converter_for_app"
            ),
            patch(
                "application_sdk.execution._temporal.worker.create_worker",
                return_value=_make_async_cm(),
            ),
            patch(
                "application_sdk.execution._temporal.auth.TemporalAuthManager",
                return_value=auth_mgr,
            ),
            patch("application_sdk.execution._temporal.auth.TemporalAuthConfig"),
            patch("application_sdk.handler.DefaultHandler"),
            patch("application_sdk.handler.create_app_handler_service"),
            patch("application_sdk.infrastructure.context.set_infrastructure"),
            patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            patch(
                "application_sdk.infrastructure.context.close_infrastructure",
                new_callable=AsyncMock,
            ),
            patch(
                "application_sdk.server.health.WorkerHealthServer",
                return_value=_make_async_cm(),
            ),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
            patch("application_sdk.main._install_graceful_signal_handlers"),
            patch("uvicorn.Server", return_value=uvicorn_server),
            patch("uvicorn.Config"),
            patch.object(asyncio.Event, "wait", new=AsyncMock(return_value=None)),
        ):
            await run_combined_mode(cfg)

        auth_mgr.acquire_initial_token.assert_awaited_once()
        auth_mgr.start_background_refresh.assert_called_once()
        auth_mgr.shutdown.assert_awaited_once()
