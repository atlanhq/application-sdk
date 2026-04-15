"""Unit tests for the main entry point."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.main import (
    AppConfig,
    _create_infrastructure,
    _derive_service_name,
    _log_dapr_components,
    parse_args,
    run_main,
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
