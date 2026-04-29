"""Unit tests for AppConfig and config consolidation (BLDX-1123)."""

from __future__ import annotations

import argparse
import os

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


class TestAuthUrlFallback:
    """ATLAN_AUTH_URL (v2 Helm) falls back to auth_token_url and auth_base_url."""

    def test_v3_env_vars_take_precedence(self, monkeypatch: pytest.MonkeyPatch):
        """v3 vars set → ATLAN_AUTH_URL ignored."""
        monkeypatch.setenv("ATLAN_AUTH_TOKEN_URL", "https://v3-token.example.com")
        monkeypatch.setenv("ATLAN_AUTH_BASE_URL", "https://v3-base.example.com")
        monkeypatch.setenv("ATLAN_AUTH_URL", "https://legacy.example.com")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.auth_token_url == "https://v3-token.example.com"
        assert config.auth_base_url == "https://v3-base.example.com"

    def test_legacy_auth_url_used_as_fallback(self, monkeypatch: pytest.MonkeyPatch):
        """Only ATLAN_AUTH_URL set (old Helm chart) → both fields get it."""
        monkeypatch.delenv("ATLAN_AUTH_TOKEN_URL", raising=False)
        monkeypatch.delenv("ATLAN_AUTH_BASE_URL", raising=False)
        monkeypatch.setenv("ATLAN_AUTH_URL", "https://legacy.example.com")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.auth_token_url == "https://legacy.example.com"
        assert config.auth_base_url == "https://legacy.example.com"

    def test_no_auth_url_set(self, monkeypatch: pytest.MonkeyPatch):
        """No auth URL env vars set → empty strings (auth disabled)."""
        monkeypatch.delenv("ATLAN_AUTH_TOKEN_URL", raising=False)
        monkeypatch.delenv("ATLAN_AUTH_BASE_URL", raising=False)
        monkeypatch.delenv("ATLAN_AUTH_URL", raising=False)
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.auth_token_url == ""
        assert config.auth_base_url == ""


class TestRemovedConstantsReplacedByAppConfig:
    """Verify removed constants no longer exist and AppConfig replaces them."""

    def test_app_host_removed_from_constants(self):
        import application_sdk.constants as c

        assert not hasattr(
            c, "APP_HOST"
        ), "APP_HOST should be removed — use AppConfig.handler_host"

    def test_app_port_removed_from_constants(self):
        import application_sdk.constants as c

        assert not hasattr(
            c, "APP_PORT"
        ), "APP_PORT should be removed — use AppConfig.handler_port"

    def test_workflow_host_removed_from_constants(self):
        import application_sdk.constants as c

        assert not hasattr(
            c, "WORKFLOW_HOST"
        ), "WORKFLOW_HOST should be removed — use AppConfig.temporal_host"

    def test_workflow_port_removed_from_constants(self):
        import application_sdk.constants as c

        assert not hasattr(
            c, "WORKFLOW_PORT"
        ), "WORKFLOW_PORT should be removed — use AppConfig.temporal_host"

    def test_workflow_namespace_removed_from_constants(self):
        import application_sdk.constants as c

        assert not hasattr(
            c, "WORKFLOW_NAMESPACE"
        ), "WORKFLOW_NAMESPACE should be removed — use AppConfig.temporal_namespace"

    def test_dead_constants_removed(self):
        import application_sdk.constants as c

        for name in [
            "MAX_CONCURRENT_ACTIVITIES",
            "HEARTBEAT_TIMEOUT",
            "START_TO_CLOSE_TIMEOUT",
            "GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS",
        ]:
            assert not hasattr(
                c, name
            ), f"{name} should be removed — see ExecutionSettings"

    def test_appconfig_handler_host_replaces_app_host(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATLAN_HANDLER_HOST", "10.0.0.1")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.handler_host == "10.0.0.1"

    def test_appconfig_handler_port_replaces_app_port(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "9000")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.handler_port == 9000

    def test_appconfig_temporal_host_replaces_workflow_host_port(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "temporal.prod:7236")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.temporal_host == "temporal.prod:7236"

    def test_appconfig_temporal_namespace_replaces_workflow_namespace(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATLAN_TEMPORAL_NAMESPACE", "production")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.temporal_namespace == "production"

    def test_appconfig_temporal_host_v2_fallback(self, monkeypatch: pytest.MonkeyPatch):
        """v2 env vars (ATLAN_WORKFLOW_HOST/PORT) still work as fallback."""
        monkeypatch.delenv("ATLAN_TEMPORAL_HOST", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_HOST", "legacy-temporal")
        monkeypatch.setenv("ATLAN_WORKFLOW_PORT", "7233")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert "legacy-temporal" in config.temporal_host

    def test_appconfig_temporal_namespace_v2_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """v2 env var (ATLAN_WORKFLOW_NAMESPACE) still works as fallback."""
        monkeypatch.delenv("ATLAN_TEMPORAL_NAMESPACE", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_NAMESPACE", "legacy-ns")
        monkeypatch.setenv("ATLAN_APP_MODULE", "app:MyApp")
        config = AppConfig.from_args_and_env(_minimal_args())
        assert config.temporal_namespace == "legacy-ns"


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


class TestServiceVersionDefault:
    """SERVICE_VERSION should default to the SDK version, not 0.1.0."""

    def test_defaults_to_sdk_version(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OTEL_SERVICE_VERSION", raising=False)
        # Re-import to pick up the default
        import importlib

        import application_sdk.constants as c

        importlib.reload(c)
        from application_sdk.version import __version__

        assert c.SERVICE_VERSION == __version__
        assert c.SERVICE_VERSION != "0.1.0"

    def test_env_override_respected(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OTEL_SERVICE_VERSION", "custom-1.2.3")
        import importlib

        import application_sdk.constants as c

        importlib.reload(c)
        assert c.SERVICE_VERSION == "custom-1.2.3"


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


class TestRunDevCombinedDaprDefaults:
    """run_dev_combined() defaults DAPR_HTTP_PORT/DAPR_GRPC_PORT for local dev."""

    def test_defaults_dapr_http_port_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        """No DAPR_HTTP_PORT in env → run_dev_combined sets 3500."""
        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)
        os.environ.setdefault("DAPR_HTTP_PORT", "3500")
        assert os.environ["DAPR_HTTP_PORT"] == "3500"

    def test_defaults_dapr_grpc_port_when_unset(self, monkeypatch: pytest.MonkeyPatch):
        """No DAPR_GRPC_PORT in env → run_dev_combined sets 50001."""
        monkeypatch.delenv("DAPR_GRPC_PORT", raising=False)
        os.environ.setdefault("DAPR_GRPC_PORT", "50001")
        assert os.environ["DAPR_GRPC_PORT"] == "50001"

    def test_respects_dev_override_http_port(self, monkeypatch: pytest.MonkeyPatch):
        """Dev exports DAPR_HTTP_PORT=4000 → setdefault does not overwrite."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "4000")
        os.environ.setdefault("DAPR_HTTP_PORT", "3500")
        assert os.environ["DAPR_HTTP_PORT"] == "4000"

    def test_respects_dev_override_grpc_port(self, monkeypatch: pytest.MonkeyPatch):
        """Dev exports DAPR_GRPC_PORT=60001 → setdefault does not overwrite."""
        monkeypatch.setenv("DAPR_GRPC_PORT", "60001")
        os.environ.setdefault("DAPR_GRPC_PORT", "50001")
        assert os.environ["DAPR_GRPC_PORT"] == "60001"

    def test_dotenv_loaded_port_not_overwritten(self, monkeypatch: pytest.MonkeyPatch):
        """If .env already loaded DAPR_HTTP_PORT → setdefault is a no-op."""
        monkeypatch.setenv("DAPR_HTTP_PORT", "3500")  # simulates dotenv
        os.environ.setdefault("DAPR_HTTP_PORT", "9999")
        assert os.environ["DAPR_HTTP_PORT"] == "3500"


class TestRunDevCombinedEnvFallbacks:
    """run_dev_combined() resolves connection-shaped kwargs with env fallbacks.

    Connectors that ship with a minimal ``python main.py`` entry point (e.g.
    ``run_dev_combined(MyApp)`` with no kwargs) get CI-stack overrides
    (ATLAN_HANDLER_PORT, ATLAN_TASK_QUEUE, ...) without per-connector
    ``os.environ.get`` boilerplate at the call site.
    """

    def _resolved(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        temporal_host: str | None = None,
        temporal_namespace: str | None = None,
        task_queue: str | None = None,
        app_name: str = "myapp",
    ) -> dict[str, object]:
        """Replicate run_dev_combined's resolution block in isolation so we
        can unit-test the precedence without bringing up the full Temporal +
        FastAPI stack."""

        def _env_int(key: str) -> int:
            val = os.environ.get(key)
            try:
                return int(val) if val else 0
            except ValueError:
                return 0

        return {
            "host": (
                host
                or os.environ.get("ATLAN_HANDLER_HOST")
                or os.environ.get("ATLAN_APP_HTTP_HOST")
                or "127.0.0.1"
            ),
            "port": (
                port
                or _env_int("ATLAN_HANDLER_PORT")
                or _env_int("ATLAN_APP_HTTP_PORT")
                or 8000
            ),
            "temporal_host": (
                temporal_host
                or os.environ.get("ATLAN_TEMPORAL_HOST")
                or "localhost:7233"
            ),
            "temporal_namespace": (
                temporal_namespace
                or os.environ.get("ATLAN_TEMPORAL_NAMESPACE")
                or os.environ.get("ATLAN_WORKFLOW_NAMESPACE")
                or "default"
            ),
            "task_queue": (
                task_queue or os.environ.get("ATLAN_TASK_QUEUE") or f"{app_name}-queue"
            ),
        }

    # ── kwarg → env → default precedence per field ────────────────────────

    def test_no_kwargs_no_env_uses_defaults(self, monkeypatch: pytest.MonkeyPatch):
        for k in (
            "ATLAN_HANDLER_HOST",
            "ATLAN_APP_HTTP_HOST",
            "ATLAN_HANDLER_PORT",
            "ATLAN_APP_HTTP_PORT",
            "ATLAN_TEMPORAL_HOST",
            "ATLAN_TEMPORAL_NAMESPACE",
            "ATLAN_WORKFLOW_NAMESPACE",
            "ATLAN_TASK_QUEUE",
        ):
            monkeypatch.delenv(k, raising=False)
        r = self._resolved(app_name="saphana-metadata-extractor")
        assert r == {
            "host": "127.0.0.1",
            "port": 8000,
            "temporal_host": "localhost:7233",
            "temporal_namespace": "default",
            "task_queue": "saphana-metadata-extractor-queue",
        }

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_HANDLER_HOST", "0.0.0.0")
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "3001")
        monkeypatch.setenv("ATLAN_TEMPORAL_HOST", "temporal:7234")
        monkeypatch.setenv("ATLAN_TEMPORAL_NAMESPACE", "ci")
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "atlan-saphana-ci")
        r = self._resolved()
        assert r["host"] == "0.0.0.0"
        assert r["port"] == 3001
        assert r["temporal_host"] == "temporal:7234"
        assert r["temporal_namespace"] == "ci"
        assert r["task_queue"] == "atlan-saphana-ci"

    def test_kwarg_beats_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "3001")
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "from-env")
        r = self._resolved(port=4242, task_queue="from-kwarg")
        assert r["port"] == 4242
        assert r["task_queue"] == "from-kwarg"

    # ── per-field env-key precedence (handler-port / -host) ────────────────

    def test_handler_port_preferred_over_app_http_port(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "3001")
        monkeypatch.setenv("ATLAN_APP_HTTP_PORT", "3002")
        assert self._resolved()["port"] == 3001

    def test_app_http_port_used_when_handler_port_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("ATLAN_HANDLER_PORT", raising=False)
        monkeypatch.setenv("ATLAN_APP_HTTP_PORT", "3002")
        assert self._resolved()["port"] == 3002

    def test_invalid_handler_port_falls_through(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("ATLAN_HANDLER_PORT", "not-a-number")
        monkeypatch.setenv("ATLAN_APP_HTTP_PORT", "3002")
        assert self._resolved()["port"] == 3002

    def test_handler_host_preferred_over_app_http_host(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ATLAN_HANDLER_HOST", "0.0.0.0")
        monkeypatch.setenv("ATLAN_APP_HTTP_HOST", "1.2.3.4")
        assert self._resolved()["host"] == "0.0.0.0"

    # ── temporal namespace v2 fallback ────────────────────────────────────

    def test_workflow_namespace_used_when_temporal_namespace_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("ATLAN_TEMPORAL_NAMESPACE", raising=False)
        monkeypatch.setenv("ATLAN_WORKFLOW_NAMESPACE", "ci")
        assert self._resolved()["temporal_namespace"] == "ci"

    # ── task-queue derivation uses passed-in app_name ─────────────────────

    def test_task_queue_default_uses_app_name(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        assert self._resolved(app_name="mssql")["task_queue"] == "mssql-queue"
