"""Unit tests for the main entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import threading
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk._loop_stall_watchdog import LoopStallWatchdog
from application_sdk._shutdown_policy import _LoopStallBound
from application_sdk.main import (
    AppConfig,
    _derive_service_name,
    _flush_observability,
    _install_excepthook,
    _install_graceful_signal_handlers,
    _log_exit_deadline_reached,
    _log_process_memory_baseline,
    _loop_exception_handler,
    _parse_workflow_max_timeout_hours,
    main,
    parse_args,
    run_combined_mode,
    run_handler_mode,
    run_main,
    run_worker_mode,
)
from application_sdk.main_errors import (
    MissingAppModuleError,
    MultiAppModuleError,
    UnknownModeError,
)
from tests.unit.private_stdlib import private_stdlib


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

    def test_empty_env_values_keep_their_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FND-5223: an empty value used to replace the default."""
        monkeypatch.setenv("ATLAN_APP_MODE", "handler")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        for key in (
            "ATLAN_TEMPORAL_NAMESPACE",
            "ATLAN_HANDLER_HOST",
            "ATLAN_LOG_LEVEL",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_NAMESPACE", "")
        monkeypatch.setenv("ATLAN_APP_HTTP_HOST", " ")
        monkeypatch.setenv("LOG_LEVEL", "")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.temporal_namespace == "default"
        assert config.handler_host == "0.0.0.0"
        assert config.log_level == "INFO"

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

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("worker", "worker"),
            (" Server ", "handler"),
            ("local", "combined"),
            ("combined", "combined"),
            ("HANDLER", "handler"),
        ],
    )
    def test_legacy_application_mode_is_case_insensitive_and_accepts_v3_names(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: str
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("APPLICATION_MODE", value)
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == expected

    def test_unrecognised_legacy_application_mode_warns_and_runs_combined(
        self, monkeypatch: pytest.MonkeyPatch, loguru_capture: list[dict]
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("APPLICATION_MODE", "bogus")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        config = AppConfig.from_args_and_env(self._make_args())
        assert config.mode == "combined"
        warnings = [r for r in loguru_capture if r["level"].name == "WARNING"]
        assert any("APPLICATION_MODE='bogus'" in r["message"] for r in warnings)

    def test_recognised_legacy_application_mode_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch, loguru_capture: list[dict]
    ) -> None:
        monkeypatch.delenv("ATLAN_APP_MODE", raising=False)
        monkeypatch.setenv("APPLICATION_MODE", "WORKER")
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        AppConfig.from_args_and_env(self._make_args())
        assert not any("APPLICATION_MODE" in r["message"] for r in loguru_capture)

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
        with pytest.raises(MissingAppModuleError) as exc_info:
            AppConfig.from_args_and_env(args)
        assert exc_info.value.code == "INVALID_INPUT_APP_MODULE_MISSING"

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

    def test_v2_temporal_tls_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Customer chart predating the v3 rename still sets the v2 name —
        # the SDK should pick it up so the deployment doesn't have to be
        # hand-edited on SDK upgrade.
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_TEMPORAL_TLS_ENABLED", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_TLS_ENABLED", "true")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.tls_enabled is True

    def test_v3_temporal_tls_enabled_takes_precedence_over_v2(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Explicit v3 setting wins even when a stale v2 value is left in the
        # environment — crucially including the v3=false case, so a customer
        # migrating TLS-on → TLS-off can flip without first scrubbing the v2
        # var from their chart.
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setenv("ATLAN_TEMPORAL_TLS_ENABLED", "false")
        monkeypatch.setenv("ATLAN_WORKFLOW_TLS_ENABLED", "true")
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.tls_enabled is False

    def test_temporal_tls_defaults_to_false_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.delenv("ATLAN_TEMPORAL_TLS_ENABLED", raising=False)
        monkeypatch.delenv("ATLAN_WORKFLOW_TLS_ENABLED", raising=False)
        args = self._make_args()
        config = AppConfig.from_args_and_env(args)
        assert config.tls_enabled is False

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


class TestParseWorkflowMaxTimeoutHours:
    """Tests for _parse_workflow_max_timeout_hours()."""

    def test_positive_value_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A positive env var value is returned as-is."""
        monkeypatch.setenv("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", "4")
        assert _parse_workflow_max_timeout_hours() == 4

    def test_zero_returns_none_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero is treated as unset — returns None without a warning."""
        monkeypatch.setenv("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", "0")
        with patch("application_sdk.main.logger") as mock_log:
            result = _parse_workflow_max_timeout_hours()
        assert result is None
        mock_log.warning.assert_not_called()

    def test_negative_returns_none_and_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A negative value returns None and emits a warning."""
        monkeypatch.setenv("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", "-5")
        with patch("application_sdk.main.logger") as mock_log:
            result = _parse_workflow_max_timeout_hours()
        assert result is None
        mock_log.warning.assert_called_once()
        assert "ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS" in mock_log.warning.call_args[0][0]

    def test_unset_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset env var returns None."""
        monkeypatch.delenv("ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS", raising=False)
        assert _parse_workflow_max_timeout_hours() is None


class TestRunMain:
    """Tests for run_main()."""

    def test_unknown_mode_raises_value_error(self) -> None:
        config = AppConfig(mode="invalid", app_module="pkg:App")
        with pytest.raises(UnknownModeError) as exc_info:
            run_main(config)
        assert exc_info.value.code == "INVALID_INPUT_MODE"
        assert exc_info.value.received_mode == "invalid"

    def test_worker_mode_calls_asyncio_run(self) -> None:
        config = AppConfig(mode="worker", app_module="pkg:App")
        with (
            private_stdlib("application_sdk.main", "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run") as mock_run,
        ):
            with mock.patch("application_sdk.main.run_worker_mode") as mode:
                mock_run.side_effect = lambda coro: None
                run_main(config)
                mock_run.assert_called_once()
                mode.assert_called_once_with(config, bound_post_drain_exit=True)

    def test_handler_mode_calls_run_handler_mode(self) -> None:
        config = AppConfig(mode="handler", app_module="pkg:App")
        with mock.patch("application_sdk.main.run_handler_mode") as mock_handler:
            run_main(config)
            mock_handler.assert_called_once_with(config)

    def test_bootstraps_tracer_provider_when_traces_enabled(self) -> None:
        """FND-4642: run_main installs the global TracerProvider beside the
        MeterProvider, so ATLAN_ENABLE_OTLP_TRACES=true exports worker spans."""
        config = AppConfig(mode="handler", app_module="pkg:App")
        with (
            mock.patch("application_sdk.constants.ENABLE_OTLP_TRACES", True),
            mock.patch(
                "application_sdk.observability.traces_adaptor.setup_tracer_provider"
            ) as mock_setup,
            mock.patch("application_sdk.main.run_handler_mode"),
        ):
            run_main(config)
        mock_setup.assert_called_once_with()

    def test_skips_tracer_provider_when_traces_disabled(self) -> None:
        config = AppConfig(mode="handler", app_module="pkg:App")
        with (
            mock.patch("application_sdk.constants.ENABLE_OTLP_TRACES", False),
            mock.patch(
                "application_sdk.observability.traces_adaptor.setup_tracer_provider"
            ) as mock_setup,
            mock.patch("application_sdk.main.run_handler_mode"),
        ):
            run_main(config)
        mock_setup.assert_not_called()

    def test_combined_mode_calls_asyncio_run(self) -> None:
        config = AppConfig(mode="combined", app_module="pkg:App")
        with (
            private_stdlib("application_sdk.main", "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run") as mock_run,
        ):
            with mock.patch("application_sdk.main.run_combined_mode") as mode:
                mock_run.side_effect = lambda coro: None
                run_main(config)
                mock_run.assert_called_once()
                mode.assert_called_once_with(config, bound_post_drain_exit=True)


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
        with pytest.raises(MultiAppModuleError) as exc_info:
            AppConfig.from_args_and_env(args)
        assert exc_info.value.code == "INVALID_INPUT_APP_MODULE_COMMA"
        assert exc_info.value.app_module == "pkg:AppA,pkg:AppB"

    def test_post_init_derives_task_queue(self) -> None:
        """__post_init__ derives task_queue from app_module when missing."""
        cfg = AppConfig(mode="worker", app_module="pkg:MyApp")
        assert cfg.task_queue == "my-app-queue"


# --------------------------------------------------------------------------- #
# _flush_observability                                                        #
# --------------------------------------------------------------------------- #


class TestFlushObservability:
    """Tests for _flush_observability()."""

    async def test_calls_atlan_observability_flush_all(self) -> None:
        """Happy path: flush_all() is awaited, then the OTLP batch is drained."""
        with (
            patch(
                "application_sdk.observability.observability.AtlanObservability.flush_all",
                new_callable=AsyncMock,
            ) as mock_flush,
            patch(
                "application_sdk.observability.logger_adaptor.flush_otlp_logs",
            ) as mock_otlp,
        ):
            await _flush_observability()
        mock_flush.assert_awaited_once()
        # CNCT-107: the OTLP BatchLogRecordProcessor queue is drained on graceful
        # shutdown, not left to the atexit backstop alone.
        mock_otlp.assert_called_once()

    async def test_swallows_exception_and_logs_warning(self) -> None:
        """If flush_all raises, the exception is swallowed and logged."""
        with (
            patch(
                "application_sdk.observability.observability.AtlanObservability.flush_all",
                new_callable=AsyncMock,
                side_effect=RuntimeError("flush failed"),
            ),
            patch(
                "application_sdk.observability.logger_adaptor.flush_otlp_logs",
            ),
            patch("application_sdk.main.logger") as mock_log,
        ):
            await _flush_observability()  # must not raise
        assert mock_log.warning.called

    async def test_swallows_otlp_drain_failure(self) -> None:
        """A failing OTLP drain is swallowed and logged; flush_all still runs."""
        with (
            patch(
                "application_sdk.observability.observability.AtlanObservability.flush_all",
                new_callable=AsyncMock,
            ) as mock_flush,
            patch(
                "application_sdk.observability.logger_adaptor.flush_otlp_logs",
                side_effect=RuntimeError("otlp drain failed"),
            ),
            patch("application_sdk.main.logger") as mock_log,
        ):
            await _flush_observability()  # must not raise
        mock_flush.assert_awaited_once()
        assert mock_log.warning.called


# --------------------------------------------------------------------------- #
# _loop_exception_handler                                                     #
# --------------------------------------------------------------------------- #


class TestLoopExceptionHandler:
    """Tests for _loop_exception_handler()."""

    @pytest.fixture(autouse=True)
    def _reset_handler_flush_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import application_sdk.main as main_mod

        monkeypatch.setattr(main_mod, "_loop_handler_flush_task", None)
        monkeypatch.setattr(main_mod, "_loop_handler_flush_last", None)

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

    def test_exception_storm_schedules_one_flush(self) -> None:
        """While a handler flush is running, further exceptions schedule no
        more: each flush takes a ``to_thread`` slot and, against a dead
        collector, an OTLP flush that only returns at its timeout."""
        loop = MagicMock()
        running = MagicMock()
        running.done.return_value = False
        loop.create_task.return_value = running
        with patch("application_sdk.main.logger"):
            for _ in range(100):
                _loop_exception_handler(loop, {"message": "storm"})
        assert loop.create_task.call_count == 1
        assert loop.default_exception_handler.call_count == 100

    def test_finished_flush_respects_min_interval(self) -> None:
        """A finished flush does not let the next exception flush at once; a
        new one starts only after the minimum interval."""
        import application_sdk.main as main_mod

        loop = MagicMock()
        finished = MagicMock()
        finished.done.return_value = True
        loop.create_task.return_value = finished
        with patch("application_sdk.main.logger"):
            _loop_exception_handler(loop, {"message": "a"})
            _loop_exception_handler(loop, {"message": "b"})
            assert loop.create_task.call_count == 1
            # Age the last flush past the interval instead of patching the
            # process-wide clock.
            main_mod._loop_handler_flush_last = (
                main_mod.time.monotonic()
                - main_mod._LOOP_HANDLER_FLUSH_MIN_INTERVAL_SECONDS
                - 1
            )
            _loop_exception_handler(loop, {"message": "c"})
        assert loop.create_task.call_count == 2


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
                private_stdlib("application_sdk.main", "asyncio") as local_asyncio,
                patch.object(local_asyncio, "run") as mock_async_run,
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
                private_stdlib("application_sdk.main", "asyncio") as local_asyncio,
                patch.object(
                    local_asyncio,
                    "run",
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

    @pytest.fixture(autouse=True)
    def _no_process_debug_handlers(self) -> Any:
        """Keep main() from installing SIGUSR1/faulthandler in the test process."""
        with patch("application_sdk.main._install_debug_handlers"):
            yield

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
            private_stdlib("application_sdk.main", "asyncio") as local_asyncio,
            patch.object(local_asyncio, "run") as mock_async_run,
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


class TestDebugHandlers:
    """faulthandler and the SIGUSR1 dump are installed by main(), not by import."""

    def test_import_installs_neither_faulthandler_nor_sigusr1(self) -> None:
        """Importing the module (dev scripts, tests) must not touch process handlers."""
        import subprocess  # noqa: PLC0415 — only this test spawns a process

        probe = (
            "import faulthandler, signal\n"
            "import application_sdk.main as m\n"
            "usr1 = getattr(signal, 'SIGUSR1', None)\n"
            "installed = usr1 is not None and "
            "signal.getsignal(usr1) is m._debug_dump_handler\n"
            "print(faulthandler.is_enabled(), installed)\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONFAULTHANDLER"}
        result = subprocess.run(  # noqa: S603 — fixed argv: this interpreter + a literal probe
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=True,
        )
        assert result.stdout.split() == ["False", "False"]

    def test_install_debug_handlers_enables_both(self) -> None:
        """main()'s installer enables faulthandler and binds SIGUSR1 to the dump."""
        import application_sdk.main as main_mod  # noqa: PLC0415 — module handle for private_stdlib

        with (
            private_stdlib(main_mod, "faulthandler") as local_fh,
            private_stdlib(main_mod, "signal") as local_signal,
            patch.object(local_fh, "enable") as enable,
            patch.object(local_signal, "signal") as bind,
        ):
            main_mod._install_debug_handlers()
        enable.assert_called_once_with()
        if hasattr(signal, "SIGUSR1"):
            bind.assert_called_once_with(signal.SIGUSR1, main_mod._debug_dump_handler)
        else:
            bind.assert_not_called()

    def test_main_installs_debug_handlers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI entry point is where the handlers get installed."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "pkg:App")
        monkeypatch.setattr(sys, "argv", ["prog", "--mode", "worker"])
        with (
            patch("application_sdk.main._install_debug_handlers") as install,
            patch("application_sdk.main._install_excepthook"),
            patch("application_sdk.main.run_main"),
            pytest.raises(SystemExit),
        ):
            main()
        install.assert_called_once_with()


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
        auth_mgr.restart_background_refresh = AsyncMock()
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

    async def test_exit_deadline_starts_after_drain_before_teardown(
        self, worker_patches, wait_patch
    ) -> None:
        order: list[str] = []
        health_cm = worker_patches["health"].return_value
        health_cm.__aexit__.side_effect = lambda *_: order.append("drained")
        worker_patches["close_infra"].side_effect = lambda: order.append("teardown")

        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline",
            side_effect=lambda: order.append("deadline"),
        ):
            await run_worker_mode(
                AppConfig(mode="worker", app_module="pkg:FakeApp"),
                bound_post_drain_exit=True,
            )

        assert order == ["drained", "deadline", "teardown"]

    async def test_exit_deadline_off_when_awaited_directly(
        self, worker_patches, wait_patch
    ) -> None:
        """A caller that awaits the mode and keeps the process gets no timer."""
        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline"
        ) as start:
            await run_worker_mode(AppConfig(mode="worker", app_module="pkg:FakeApp"))

        start.assert_not_called()

    async def test_stall_watchdog_is_set_for_shutdown_only(
        self, worker_patches, wait_patch
    ) -> None:
        """Chained after the loop's signal handlers, re-armed by the loop's
        shutdown handler, and handed over to the post-drain deadline."""
        order: list[str] = []
        handlers: list[Any] = []
        watchdog = MagicMock(spec=LoopStallWatchdog)
        watchdog.timeout_seconds = 75
        watchdog.install.side_effect = lambda *_: order.append("set for shutdown")
        watchdog.stop.side_effect = lambda: order.append("watchdog off")
        health_cm = worker_patches["health"].return_value
        health_cm.__aexit__.side_effect = lambda *_: order.append("drained")

        def _install(loop: Any, handler: Any) -> None:
            order.append("loop handlers")
            handlers.append(handler)

        with (
            patch(
                "application_sdk.main.LoopStallWatchdog", return_value=watchdog
            ) as cls,
            patch(
                "application_sdk.main._loop_stall_bound",
                return_value=_LoopStallBound(
                    75, 60, None, overridden=False, exit_by_seconds=300
                ),
            ),
            patch(
                "application_sdk.main._install_graceful_signal_handlers",
                side_effect=_install,
            ),
            patch(
                "application_sdk._shutdown_policy._start_post_drain_exit_deadline",
                side_effect=lambda: order.append("deadline"),
            ),
        ):
            await run_worker_mode(
                AppConfig(mode="worker", app_module="pkg:FakeApp"),
                bound_post_drain_exit=True,
            )
            watchdog.start_rearming.assert_not_called()
            handlers[0]()

        cls.assert_called_once_with(
            75, 300, on_exit_deadline=_log_exit_deadline_reached
        )
        assert order == [
            "loop handlers",
            "set for shutdown",
            "drained",
            "watchdog off",
            "deadline",
        ]
        watchdog.start_rearming.assert_called_once_with()

    async def test_stall_watchdog_off_when_awaited_directly(
        self, worker_patches, wait_patch
    ) -> None:
        """It ends the process, so a caller that keeps the process gets none."""
        with patch("application_sdk.main.LoopStallWatchdog") as cls:
            await run_worker_mode(AppConfig(mode="worker", app_module="pkg:FakeApp"))

        cls.assert_not_called()

    async def test_stall_watchdog_off_when_the_bound_is_zero(
        self, worker_patches, wait_patch
    ) -> None:
        with (
            patch("application_sdk.main.LoopStallWatchdog") as cls,
            patch(
                "application_sdk.main._loop_stall_bound",
                return_value=_LoopStallBound(0, 60, None, overridden=True),
            ),
        ):
            await run_worker_mode(
                AppConfig(mode="worker", app_module="pkg:FakeApp"),
                bound_post_drain_exit=True,
            )

        cls.assert_not_called()

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

    async def test_activity_recorder_wired_into_worker(self, worker_patches) -> None:
        """run_worker_mode wires the health server's record_activity into
        create_worker (via the _build_worker factory) so the /live liveness
        window reflects real worker progress (BLDX-1552)."""
        health_cm = _make_async_cm()
        worker_patches["health"].return_value = health_cm

        captured: dict = {}

        def _capture_create_worker(*args, **kwargs):
            captured["on_activity"] = kwargs.get("on_activity")
            return _make_async_cm()

        cfg = AppConfig(mode="worker", app_module="pkg:FakeApp")
        with (
            patch(
                "application_sdk.execution._temporal.worker.create_worker",
                side_effect=_capture_create_worker,
            ),
            patch.object(asyncio.Event, "wait", new=AsyncMock(return_value=None)),
        ):
            await run_worker_mode(cfg)

        from application_sdk.constants import WORKER_LIVENESS_MAX_IDLE_SECONDS

        # Identity check only — that the exact recorder is wired through; the
        # callback's runtime behavior is exercised by the interceptor tests.
        assert captured["on_activity"] is health_cm.record_activity
        # The liveness window must reach the constructor: a dropped kwarg would
        # silently never activate /live yet leave the on_activity wiring intact.
        assert (
            worker_patches["health"].call_args.kwargs["max_idle_seconds"]
            == WORKER_LIVENESS_MAX_IDLE_SECONDS
        )

    async def test_reconnect_closure_rebinds_client_and_health(
        self, worker_patches, auth_manager
    ) -> None:
        """The wired reconnect runs the production helper, not a test stub.

        After a successful reconnect the next worker must be built on the new
        client and /ready must follow that client. shutdown() must not run
        before the replacement connect succeeds.
        """
        old_client = object()
        new_client = object()
        worker_patches["create_client"].side_effect = [old_client, new_client]
        auth_manager.acquire_initial_token.side_effect = ["api-key-xyz", "api-key-new"]
        health_cm = _make_async_cm()
        worker_patches["health"].return_value = health_cm

        captured_clients: list[object] = []
        calls = {"n": 0}
        reconnect_holder: dict[str, Any] = {}

        def _capture_create_worker(client, *args, **kwargs):
            captured_clients.append(client)
            return _make_async_cm()

        async def _capture_supervise(**kwargs: Any) -> None:
            reconnect_holder["fn"] = kwargs["reconnect"]
            build = kwargs["build_worker"]
            calls["n"] += 1
            build()
            await kwargs["reconnect"]()
            calls["n"] += 1
            build()

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
            patch(
                "application_sdk.execution._temporal.worker.create_worker",
                side_effect=_capture_create_worker,
            ),
            patch(
                "application_sdk.execution._temporal.supervisor._supervise_worker",
                side_effect=_capture_supervise,
            ),
        ):
            await run_worker_mode(cfg)
            # Drive the production closure a second time after the mode returns
            # so we also cover a later restart generation.
            assert reconnect_holder["fn"] is not None

        assert captured_clients == [old_client, new_client]
        assert auth_manager.acquire_initial_token.await_count == 2
        auth_manager.start_background_refresh.assert_called_once_with(old_client)
        auth_manager.restart_background_refresh.assert_awaited_once_with(new_client)
        health_cm.set_temporal_client.assert_any_call(new_client)
        # Process teardown still shuts the manager down once at the end.
        assert auth_manager.shutdown.await_count == 1

    async def test_reconnect_connect_failure_does_not_stop_refresh(
        self, worker_patches, auth_manager
    ) -> None:
        """A failed replacement connect must leave the existing refresh loop up."""
        old_client = object()
        worker_patches["create_client"].side_effect = [
            old_client,
            RuntimeError("temporal unreachable"),
        ]
        auth_manager.acquire_initial_token.side_effect = ["api-key-xyz", "api-key-new"]

        async def _fail_on_reconnect(**kwargs: Any) -> None:
            with pytest.raises(RuntimeError, match="temporal unreachable"):
                await kwargs["reconnect"]()

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
            patch(
                "application_sdk.execution._temporal.supervisor._supervise_worker",
                side_effect=_fail_on_reconnect,
            ),
        ):
            await run_worker_mode(cfg)

        # Initial start only — reconnect must not have stopped the loop.
        auth_manager.start_background_refresh.assert_called_once()
        # shutdown is process teardown at the end of run_worker_mode, not the
        # failed reconnect (which must leave the loop running until then).
        assert auth_manager.shutdown.await_count == 1


# --------------------------------------------------------------------------- #
# run_handler_mode                                                            #
# --------------------------------------------------------------------------- #


class _FakeUvicornServer:
    """Stands in for ``uvicorn.Server``: ``serve()`` returns at once."""

    async def serve(self, sockets: Any = None) -> None:
        return None


class _FakeDaprSidecar:
    """A keep-alive HTTP/1.1 Dapr stand-in on loopback TCP, on its own thread.

    TCP, not a unix socket: anyio's unix-socket stream is not tied to an event
    loop, so only a TCP keep-alive connection reproduces the cross-loop
    failure. Serving from a separate thread matches a real sidecar: the
    client's pooled connection is the only thing tied to the caller's loop.
    """

    def __init__(self, responses: dict[str, bytes]) -> None:
        import http.server

        paths: list[str] = []
        self.paths = paths

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive, like the Dapr sidecar

            def do_GET(self) -> None:
                paths.append(self.path)
                body = responses.get(self.path, b"{}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_FakeDaprSidecar":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


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
            patch(
                "application_sdk.storage.preflight.verify_object_store_access",
                new_callable=AsyncMock,
            ),
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
        p_infra, p_load_app, p_conv, p_set, p_pre, p_validate = self._common_patches(
            infra
        )

        # asyncio.run must execute coroutines (so _create_infrastructure returns infra)
        # but we don't want _flush_observability's coroutine to run real code.
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_pre,
            p_validate,
            patch(
                "application_sdk.main.load_handler_class",
                return_value=None,
            ),
            patch(
                "application_sdk.handler.DefaultHandler",
                return_value=MagicMock(),
            ) as mock_default,
            patch(
                "application_sdk.handler.create_app_handler_service"
            ) as mock_create_svc,
            patch("uvicorn.Server", return_value=_FakeUvicornServer()),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
        ):
            cfg = AppConfig(mode="handler", app_module="pkg:FakeApp")
            run_handler_mode(cfg)

        mock_default.assert_called_once()
        mock_create_svc.assert_called_once()
        kwargs = mock_create_svc.call_args.kwargs
        assert kwargs["enable_temporal_core_metrics"] is False
        assert kwargs["prometheus_bind_address"] == "127.0.0.1:9464"

    def test_uses_custom_handler_when_loaded(self) -> None:
        """When load_handler_class returns a class, that class is instantiated."""
        infra = self._make_infra()
        p_infra, p_load_app, p_conv, p_set, p_pre, p_validate = self._common_patches(
            infra
        )
        custom_cls = MagicMock()
        custom_cls.__name__ = "MyCustomHandler"
        custom_cls.return_value = MagicMock()

        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_pre,
            p_validate,
            patch(
                "application_sdk.main.load_handler_class",
                return_value=custom_cls,
            ),
            patch("application_sdk.handler.create_app_handler_service"),
            patch("uvicorn.Server", return_value=_FakeUvicornServer()),
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
        p_infra, p_load_app, p_conv, p_set, p_pre, p_validate = self._common_patches(
            infra
        )
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_pre,
            p_validate,
            patch("application_sdk.main.validate_app_class") as mock_validate,
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch("application_sdk.handler.DefaultHandler"),
            patch("application_sdk.handler.create_app_handler_service"),
            patch("uvicorn.Server", return_value=_FakeUvicornServer()),
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
        p_infra, p_load_app, p_conv, p_set, p_pre, p_validate = self._common_patches(
            infra
        )
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_pre,
            p_validate,
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch("application_sdk.handler.DefaultHandler"),
            patch("application_sdk.handler.create_app_handler_service"),
            patch("uvicorn.Server", return_value=_FakeUvicornServer()),
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

    # Loopback only: any connect to another host is still refused.
    @pytest.mark.allow_hosts(["127.0.0.1"])
    def test_dapr_client_built_used_and_closed_on_one_loop(self) -> None:
        """The startup Dapr call, a request's secret fetch and the shutdown close
        all succeed with the real ``AsyncDaprClient``.

        The client's pooled keep-alive connection belongs to the loop that
        opened it. When handler mode created infrastructure, served and closed
        on three ``asyncio.run`` loops, the first secret fetch failed with
        ``Event loop is closed`` and ``close_infrastructure`` raised the same.
        """
        import httpx
        import uvicorn

        from application_sdk.infrastructure._dapr import http as dapr_http
        from application_sdk.infrastructure._dapr.client import DaprSecretStore
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            get_infrastructure,
        )

        loops: dict[str, asyncio.AbstractEventLoop] = {}
        fetched: list[str] = []

        async def _fake_create_infrastructure() -> InfrastructureContext:
            loops["create"] = asyncio.get_running_loop()
            client = dapr_http.AsyncDaprClient(base_url=sidecar.base_url, retries=0)
            await client.get_metadata()  # the startup metadata call
            return InfrastructureContext(
                secret_store=DaprSecretStore(client), _dapr_client=client
            )

        async def _serve_fetches_secret(self: Any, sockets: Any = None) -> None:
            loops["serve"] = asyncio.get_running_loop()
            self.started = True  # what uvicorn.run checks after serve() returns
            infra = get_infrastructure()
            assert infra is not None and infra.secret_store is not None
            fetched.append(await infra.secret_store.get("cred"))

        responses = {
            "/v1.0/metadata": b"{}",
            "/v1.0/secrets/secretstore/cred": b'{"cred": "value"}',
        }
        with (
            _FakeDaprSidecar(responses) as sidecar,
            # Undo the unit-tier sidecar interceptor for this test only:
            # the real pooled transport is what this test is about.
            patch.object(
                dapr_http.httpx, "AsyncHTTPTransport", httpx.AsyncHTTPTransport
            ),
            patch(
                "application_sdk.main._create_infrastructure",
                _fake_create_infrastructure,
            ),
            patch(
                "application_sdk.storage.preflight.verify_object_store_access",
                new_callable=AsyncMock,
            ),
            patch(
                "application_sdk.main.load_app_class",
                return_value=_fake_app_class(),
            ),
            patch("application_sdk.main.validate_app_class"),
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch("application_sdk.handler.DefaultHandler"),
            patch(
                "application_sdk.execution._temporal.converter.create_data_converter_for_app"
            ),
            patch("application_sdk.handler.create_app_handler_service"),
            patch("application_sdk.handler.service.create_app_handler_service"),
            # Patch the method, not the class: the request path runs on
            # whatever loop serves, however the server is started.
            patch.object(uvicorn.Server, "serve", _serve_fetches_secret),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ) as mock_flush,
        ):
            run_handler_mode(AppConfig(mode="handler", app_module="pkg:FakeApp"))

        assert fetched == ["value"]
        assert loops["create"] is loops["serve"]
        assert sidecar.paths == ["/v1.0/metadata", "/v1.0/secrets/secretstore/cred"]
        assert get_infrastructure() is None
        mock_flush.assert_awaited_once()

    def test_flushes_observability_when_close_infrastructure_raises(self) -> None:
        """A failing close must not skip the final observability flush."""
        infra = self._make_infra()
        p_infra, p_load_app, p_conv, p_set, p_pre, p_validate = self._common_patches(
            infra
        )
        with (
            p_infra,
            p_load_app,
            p_conv,
            p_set,
            p_pre,
            p_validate,
            patch("application_sdk.main.load_handler_class", return_value=None),
            patch("application_sdk.handler.DefaultHandler"),
            patch("application_sdk.handler.create_app_handler_service"),
            patch("uvicorn.Server", return_value=_FakeUvicornServer()),
            patch(
                "application_sdk.infrastructure.context.close_infrastructure",
                new_callable=AsyncMock,
                side_effect=RuntimeError("close failed"),
            ),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ) as mock_flush,
            pytest.raises(RuntimeError, match="close failed"),
        ):
            run_handler_mode(AppConfig(mode="handler", app_module="pkg:FakeApp"))
        mock_flush.assert_awaited_once()


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
            ) as mock_health,
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
                "health": mock_health,
            }

    async def test_combined_runs_to_completion(self, combined_patches) -> None:
        """Combined mode creates client, FastAPI app, runs uvicorn, closes infra."""
        cfg = AppConfig(mode="combined", app_module="pkg:FakeApp")
        await run_combined_mode(cfg)
        combined_patches["create_client"].assert_awaited_once()
        combined_patches["create_svc"].assert_called_once()
        kwargs = combined_patches["create_svc"].call_args.kwargs
        assert kwargs["enable_temporal_core_metrics"] is True
        assert kwargs["prometheus_bind_address"] == "127.0.0.1:9464"
        # FND-4644: the handler port's /ready mirrors the co-hosted worker's
        # health server, so both ports give one answer about the pod.
        assert kwargs["worker_health"] is combined_patches["health"].return_value
        combined_patches["uvicorn_server"].serve.assert_awaited()
        combined_patches["close_infra"].assert_awaited_once()

    async def test_exit_deadline_starts_after_drain_before_teardown(
        self, combined_patches
    ) -> None:
        order: list[str] = []
        health_cm = combined_patches["health"].return_value
        health_cm.__aexit__.side_effect = lambda *_: order.append("drained")
        combined_patches["close_infra"].side_effect = lambda: order.append("teardown")

        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline",
            side_effect=lambda: order.append("deadline"),
        ):
            await run_combined_mode(
                AppConfig(mode="combined", app_module="pkg:FakeApp"),
                bound_post_drain_exit=True,
            )

        assert order == ["drained", "deadline", "teardown"]

    async def test_exit_deadline_off_when_awaited_directly(
        self, combined_patches
    ) -> None:
        """A caller that awaits the mode and keeps the process gets no timer."""
        with patch(
            "application_sdk._shutdown_policy._start_post_drain_exit_deadline"
        ) as start:
            await run_combined_mode(
                AppConfig(mode="combined", app_module="pkg:FakeApp")
            )

        start.assert_not_called()

    async def test_stall_watchdog_is_set_for_shutdown_only(
        self, combined_patches
    ) -> None:
        """As in worker mode, and uvicorn's own exit handler arms it too: while
        serving, uvicorn replaces the process-level signal handlers."""
        order: list[str] = []
        watchdog = MagicMock(spec=LoopStallWatchdog)
        watchdog.timeout_seconds = 75
        watchdog.install.side_effect = lambda *_: order.append("set for shutdown")
        watchdog.stop.side_effect = lambda: order.append("watchdog off")
        health_cm = combined_patches["health"].return_value
        health_cm.__aexit__.side_effect = lambda *_: order.append("drained")
        uvicorn_server = combined_patches["uvicorn_server"]
        uvicorn_exit = uvicorn_server.handle_exit

        with (
            patch(
                "application_sdk.main.LoopStallWatchdog", return_value=watchdog
            ) as cls,
            patch(
                "application_sdk.main._loop_stall_bound",
                return_value=_LoopStallBound(
                    75, 60, None, overridden=False, exit_by_seconds=300
                ),
            ),
            patch(
                "application_sdk._shutdown_policy._start_post_drain_exit_deadline",
                side_effect=lambda: order.append("deadline"),
            ),
        ):
            await run_combined_mode(
                AppConfig(mode="combined", app_module="pkg:FakeApp"),
                bound_post_drain_exit=True,
            )

        cls.assert_called_once_with(
            75, 300, on_exit_deadline=_log_exit_deadline_reached
        )
        assert order == ["set for shutdown", "drained", "watchdog off", "deadline"]
        uvicorn_server.handle_exit(signal.SIGTERM, None)
        watchdog.on_shutdown_signal.assert_called_once_with()
        uvicorn_exit.assert_called_once_with(signal.SIGTERM, None)

    async def test_stall_watchdog_off_when_awaited_directly(
        self, combined_patches
    ) -> None:
        """It ends the process, so a caller that keeps the process gets none."""
        with patch("application_sdk.main.LoopStallWatchdog") as cls:
            await run_combined_mode(
                AppConfig(mode="combined", app_module="pkg:FakeApp")
            )

        cls.assert_not_called()

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

    async def test_activity_recorder_wired_into_worker(self, combined_patches) -> None:
        """run_combined_mode wires the health server's record_activity into
        create_worker (via the _build_worker factory) so the /live liveness
        window reflects real worker progress in combined mode (BLDX-1552).

        Mirrors TestRunWorkerMode.test_activity_recorder_wired_into_worker so a
        dropped kwarg can't silently disable the liveness window in combined mode."""
        health_cm = _make_async_cm()
        combined_patches["health"].return_value = health_cm

        captured: dict = {}

        def _capture_create_worker(*args, **kwargs):
            captured["on_activity"] = kwargs.get("on_activity")
            return _make_async_cm()

        cfg = AppConfig(mode="combined", app_module="pkg:FakeApp")
        with patch(
            "application_sdk.execution._temporal.worker.create_worker",
            side_effect=_capture_create_worker,
        ):
            await run_combined_mode(cfg)

        from application_sdk.constants import WORKER_LIVENESS_MAX_IDLE_SECONDS

        # Identity check only — that the exact recorder is wired through; the
        # callback's runtime behavior is exercised by the interceptor tests.
        assert captured["on_activity"] is health_cm.record_activity
        # The liveness window must reach the constructor: a dropped kwarg would
        # silently never activate /live yet leave the on_activity wiring intact.
        assert (
            combined_patches["health"].call_args.kwargs["max_idle_seconds"]
            == WORKER_LIVENESS_MAX_IDLE_SECONDS
        )


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
            ("application_sdk.observability.observability", "AtlanObservability"),
            ("application_sdk.server.health", "WorkerHealthServer"),
            ("application_sdk.storage", "create_store_from_binding_with_put_attrs"),
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

    async def test_reconnect_closure_rebinds_client_and_health(self) -> None:
        """Combined-mode reconnect uses the same production helper as worker mode."""
        infra = MagicMock()
        infra.secret_store = MagicMock()
        infra.storage = MagicMock()

        uvicorn_server = MagicMock()
        uvicorn_server.serve = AsyncMock(return_value=None)

        auth_mgr = MagicMock()
        auth_mgr.acquire_initial_token = AsyncMock(
            side_effect=["api-key", "api-key-new"]
        )
        auth_mgr.start_background_refresh = MagicMock()
        auth_mgr.restart_background_refresh = AsyncMock()
        auth_mgr.shutdown = AsyncMock()

        old_client = object()
        new_client = object()
        health_cm = _make_async_cm()
        captured_clients: list[object] = []

        def _capture_create_worker(client, *args, **kwargs):
            captured_clients.append(client)
            return _make_async_cm()

        async def _capture_supervise(**kwargs: Any) -> None:
            build = kwargs["build_worker"]
            build()
            await kwargs["reconnect"]()
            build()

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
                side_effect=[old_client, new_client],
            ),
            patch(
                "application_sdk.execution._temporal.converter.create_data_converter_for_app"
            ),
            patch(
                "application_sdk.execution._temporal.worker.create_worker",
                side_effect=_capture_create_worker,
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
                return_value=health_cm,
            ),
            patch(
                "application_sdk.main._flush_observability",
                new_callable=AsyncMock,
            ),
            patch("application_sdk.main._install_graceful_signal_handlers"),
            patch("uvicorn.Server", return_value=uvicorn_server),
            patch("uvicorn.Config"),
            patch(
                "application_sdk.execution._temporal.supervisor._supervise_worker",
                side_effect=_capture_supervise,
            ),
        ):
            await run_combined_mode(cfg)

        assert captured_clients == [old_client, new_client]
        auth_mgr.restart_background_refresh.assert_awaited_once_with(new_client)
        health_cm.set_temporal_client.assert_any_call(new_client)
        assert auth_mgr.shutdown.await_count == 1


# ---------------------------------------------------------------------------
# _log_process_memory_baseline
# ---------------------------------------------------------------------------


class TestLogProcessMemoryBaseline:
    def test_emits_info_with_percentage(self, monkeypatch) -> None:
        """INFO log is emitted with RSS/limit ratio when both are available."""
        import application_sdk.main as main_mod
        from application_sdk.observability.resource_sampler import ResourceSample

        limit = 8 * 1024**3  # 8 GiB
        rss = 2 * 1024**3  # 2 GiB → 25%
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", str(limit))

        with (
            patch(
                "application_sdk.observability.resource_sampler.sample",
                return_value=ResourceSample(cpu_time_s=0.5, rss_bytes=rss),
            ),
            patch.object(main_mod, "logger") as mock_logger,
        ):
            _log_process_memory_baseline()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if "memory at start" in str(c).lower()
        ]
        assert info_calls, "Expected INFO memory baseline log"
        # args: (fmt, rss_gib, limit_gib, pct_float)
        _fmt, _rss, _lim, pct = info_calls[0].args
        assert abs(pct - 25.0) < 0.5

    def test_silent_when_limit_unset(self, monkeypatch) -> None:
        """No INFO log when K8S_POD_MEMORY_LIMIT is absent (local dev / non-K8s)."""
        import application_sdk.main as main_mod

        monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)

        with patch.object(main_mod, "logger") as mock_logger:
            _log_process_memory_baseline()

        mock_logger.info.assert_not_called()

    def test_silent_when_sample_returns_none(self, monkeypatch) -> None:
        """No INFO log when resource sampling fails (e.g. on Windows)."""
        import application_sdk.main as main_mod

        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", str(4 * 1024**3))

        with (
            patch(
                "application_sdk.observability.resource_sampler.sample",
                return_value=None,
            ),
            patch.object(main_mod, "logger") as mock_logger,
        ):
            _log_process_memory_baseline()

        mock_logger.info.assert_not_called()
