"""Unit tests for AppConfig and config consolidation (BLDX-1123)."""

from __future__ import annotations

import argparse

import pytest

from application_sdk.main import AppConfig


def _minimal_args(**overrides: str) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for AppConfig.from_args_and_env."""
    defaults = {
        "mode": "",
        "app": "app.my_app:MyApp",
        "handler": None,
        "temporal_host": None,
        "temporal_namespace": None,
        "task_queue": None,
        "handler_host": None,
        "handler_port": None,
        "log_level": None,
        "health_port": None,
        "service_name": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestAppConfigDefaults:
    """Default values for new runtime flags."""

    def test_enable_prometheus_metrics_default_true(self):
        config = AppConfig(mode="worker", app_module="app:MyApp")
        assert config.enable_prometheus_metrics is True

    def test_prometheus_bind_address_default(self):
        config = AppConfig(mode="worker", app_module="app:MyApp")
        assert config.prometheus_bind_address == "0.0.0.0:9464"

    def test_enable_app_vitals_default_true(self):
        config = AppConfig(mode="worker", app_module="app:MyApp")
        assert config.enable_app_vitals is True

    def test_enable_mcp_default_false(self):
        config = AppConfig(mode="worker", app_module="app:MyApp")
        assert config.enable_mcp is False

    def test_max_concurrent_storage_transfers_default(self):
        config = AppConfig(mode="worker", app_module="app:MyApp")
        assert config.max_concurrent_storage_transfers == 4


class TestAppConfigFromEnv:
    """Environment variable fallbacks for runtime flags."""

    def test_prometheus_enabled_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_ENABLE_PROMETHEUS_METRICS", "false")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_prometheus_metrics is False

    def test_prometheus_bind_address_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS", "0.0.0.0:9999")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.prometheus_bind_address == "0.0.0.0:9999"

    def test_app_vitals_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "false")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_app_vitals is False

    def test_mcp_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ENABLE_MCP", "true")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_mcp is True

    def test_storage_transfers_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS", "8")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.max_concurrent_storage_transfers == 8

    def test_prometheus_default_true_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Prod behavior: ATLAN_ENABLE_PROMETHEUS_METRICS not set → True."""
        monkeypatch.delenv("ATLAN_ENABLE_PROMETHEUS_METRICS", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_prometheus_metrics is True


class TestAppConfigOverlappingConstants:
    """Verify AppConfig reads the same env vars as deprecated constants."""

    def test_handler_host_matches_constant_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_HANDLER_HOST", "10.0.0.1")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.handler_host == "10.0.0.1"

    def test_handler_port_matches_constant_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "9090")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.handler_port == 9090

    def test_temporal_host_matches_constant_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "temporal.prod:7236")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.temporal_host == "temporal.prod:7236"

    def test_log_level_matches_constant_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_LOG_LEVEL", "WARNING")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.log_level == "WARNING"


class TestDeadConstantsRemoved:
    """Verify dead constants no longer exist in constants.py."""

    def test_max_concurrent_activities_removed(self):
        from application_sdk import constants

        assert not hasattr(constants, "MAX_CONCURRENT_ACTIVITIES")

    def test_heartbeat_timeout_removed(self):
        from application_sdk import constants

        assert not hasattr(constants, "HEARTBEAT_TIMEOUT")

    def test_start_to_close_timeout_removed(self):
        from application_sdk import constants

        assert not hasattr(constants, "START_TO_CLOSE_TIMEOUT")

    def test_graceful_shutdown_timeout_removed(self):
        from application_sdk import constants

        assert not hasattr(constants, "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS")


class TestRealWorldDevScenarios:
    """Simulate real developer workflows using AppConfig + env vars."""

    def test_local_dev_with_dotenv(self, monkeypatch: pytest.MonkeyPatch):
        """Dev has .env with: ATLAN_ENABLE_PROMETHEUS_METRICS=false
        → AppConfig picks it up, no port collision on hot reload."""
        monkeypatch.setenv("ATLAN_ENABLE_PROMETHEUS_METRICS", "false")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_prometheus_metrics is False

    def test_prod_deployment_no_env_override(self, monkeypatch: pytest.MonkeyPatch):
        """Prod: ATLAN_ENABLE_PROMETHEUS_METRICS not set → defaults to True.
        KEDA scrapes :9464 as expected."""
        monkeypatch.delenv("ATLAN_ENABLE_PROMETHEUS_METRICS", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_prometheus_metrics is True
        assert config.prometheus_bind_address == "0.0.0.0:9464"

    def test_prod_deployment_with_helm_env(self, monkeypatch: pytest.MonkeyPatch):
        """Prod: Helm chart sets ATLAN_ENABLE_PROMETHEUS_METRICS=true explicitly."""
        monkeypatch.setenv("ATLAN_ENABLE_PROMETHEUS_METRICS", "true")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_prometheus_metrics is True

    def test_dev_custom_prometheus_port(self, monkeypatch: pytest.MonkeyPatch):
        """Dev wants Prometheus on a different port to avoid conflicts."""
        monkeypatch.setenv("ATLAN_ENABLE_PROMETHEUS_METRICS", "true")
        monkeypatch.setenv("ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS", "127.0.0.1:9999")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_prometheus_metrics is True
        assert config.prometheus_bind_address == "127.0.0.1:9999"

    def test_dev_disables_app_vitals_for_debugging(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Dev disables vitals interceptor to reduce log noise during debugging."""
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "false")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_app_vitals is False

    def test_dev_enables_mcp_server(self, monkeypatch: pytest.MonkeyPatch):
        """Dev enables MCP server for AI-assisted development."""
        monkeypatch.setenv("ENABLE_MCP", "true")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.enable_mcp is True

    def test_prod_custom_storage_concurrency(self, monkeypatch: pytest.MonkeyPatch):
        """Prod tunes storage concurrency for high-throughput connector."""
        monkeypatch.setenv("ATLAN_MAX_CONCURRENT_STORAGE_TRANSFERS", "16")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.max_concurrent_storage_transfers == 16

    def test_full_prod_env(self, monkeypatch: pytest.MonkeyPatch):
        """Simulate a full production environment with all env vars set."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.connector:SnowflakeApp")
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "temporal.prod.svc:7236")
        monkeypatch.setenv("ATLAN_TEMPORAL_NAMESPACE", "production")
        monkeypatch.setenv("ATLAN_HANDLER_HOST", "0.0.0.0")
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "8000")
        monkeypatch.setenv("ATLAN_LOG_LEVEL", "INFO")
        monkeypatch.setenv("ATLAN_ENABLE_PROMETHEUS_METRICS", "true")
        monkeypatch.setenv("ATLAN_ENABLE_APP_VITALS", "true")
        monkeypatch.setenv("ATLAN_AUTH_ENABLED", "true")

        config = AppConfig.from_args_and_env(
            _minimal_args(app="app.connector:SnowflakeApp")
        )
        assert config.temporal_host == "temporal.prod.svc:7236"
        assert config.temporal_namespace == "production"
        assert config.handler_host == "0.0.0.0"
        assert config.handler_port == 8000
        assert config.log_level == "INFO"
        assert config.enable_prometheus_metrics is True
        assert config.enable_app_vitals is True
        assert config.auth_enabled is True

    def test_full_local_dev_env(self, monkeypatch: pytest.MonkeyPatch):
        """Simulate a local dev environment with minimal env vars."""
        monkeypatch.setenv("ATLAN_APP_MODULE", "app.my_app:MyApp")
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")
        monkeypatch.setenv("ATLAN_ENABLE_PROMETHEUS_METRICS", "false")
        monkeypatch.delenv("ATLAN_TEMPORAL_HOST", raising=False)
        monkeypatch.delenv("ATLAN_AUTH_ENABLED", raising=False)

        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.temporal_host == "localhost:7233"  # default
        assert config.enable_prometheus_metrics is False
        assert config.auth_enabled is False  # default
        assert config.log_level == "INFO"  # default

    def test_cli_args_override_env(self, monkeypatch: pytest.MonkeyPatch):
        """CLI args take precedence over env vars."""
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "temporal.env:7236")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")

        config = AppConfig.from_args_and_env(
            _minimal_args(temporal_host="temporal.cli:7233")
        )
        assert config.temporal_host == "temporal.cli:7233"


class TestDaprPortExport:
    """Verify poe start-deps exports Dapr ports in the shell."""

    def test_start_deps_exports_dapr_http_port(self):
        """pyproject.toml start-deps task must export DAPR_HTTP_PORT."""
        from pathlib import Path

        pyproject = Path("pyproject.toml").read_text()
        assert "export DAPR_HTTP_PORT=3500" in pyproject

    def test_start_deps_exports_dapr_grpc_port(self):
        """pyproject.toml start-deps task must export DAPR_GRPC_PORT."""
        from pathlib import Path

        pyproject = Path("pyproject.toml").read_text()
        assert "DAPR_GRPC_PORT=50001" in pyproject
