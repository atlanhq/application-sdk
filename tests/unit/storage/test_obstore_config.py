"""Tests for obstore client_options and retry_config plumbing (BLDX-1155 #3).

A production RCA showed that ``S3Store`` was being created with nothing but
region+credentials — which means object_store-rs falls back to defaults sized
for small objects (request timeout 30 s).  These tests pin the env-var →
ClientConfig contract so large files do not silently inherit a small-object
timeout.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from application_sdk.storage._obstore_config import (
    log_obstore_config,
    obstore_client_options,
    obstore_retry_config,
)

# ---------------------------------------------------------------------------
# obstore_client_options()
# ---------------------------------------------------------------------------


class TestClientOptionsDefaults:
    """SDK defaults are never empty — every store must inherit sane values."""

    def test_defaults_set_per_request_timeout_to_90_seconds(self, monkeypatch) -> None:
        for k in [
            "ATLAN_OBSTORE_TIMEOUT",
            "ATLAN_OBSTORE_CONNECT_TIMEOUT",
            "ATLAN_OBSTORE_POOL_IDLE_TIMEOUT",
            "ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT",
            "ATLAN_OBSTORE_USER_AGENT",
            "ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST",
        ]:
            monkeypatch.delenv(k, raising=False)

        opts = obstore_client_options()
        assert opts["timeout"] == "90s"
        assert opts["connect_timeout"] == "30s"
        assert opts["pool_idle_timeout"] == "90s"
        assert opts["http2_keep_alive_timeout"] == "30s"
        assert opts["user_agent"].startswith("atlan-application-sdk")

    def test_pool_max_idle_per_host_omitted_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST", raising=False)
        opts = obstore_client_options()
        assert "pool_max_idle_per_host" not in opts


class TestClientOptionsOverrides:
    def test_each_field_can_be_overridden_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_TIMEOUT", "1h")
        monkeypatch.setenv("ATLAN_OBSTORE_CONNECT_TIMEOUT", "10s")
        monkeypatch.setenv("ATLAN_OBSTORE_POOL_IDLE_TIMEOUT", "120s")
        monkeypatch.setenv("ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT", "45s")
        monkeypatch.setenv("ATLAN_OBSTORE_USER_AGENT", "custom-agent/1.0")
        monkeypatch.setenv("ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST", "32")

        # Pin no proxy so the exact-dict assertion is deterministic regardless
        # of the host's proxy env / system settings.
        with patch("urllib.request.getproxies", return_value={}):
            opts = obstore_client_options()
        assert opts == {
            "timeout": "1h",
            "connect_timeout": "10s",
            "pool_idle_timeout": "120s",
            "http2_keep_alive_timeout": "45s",
            "user_agent": "custom-agent/1.0",
            "pool_max_idle_per_host": "32",
        }


# ---------------------------------------------------------------------------
# proxy_url plumbing (obstore does not read proxy env vars itself)
# ---------------------------------------------------------------------------


class TestClientOptionsProxy:
    """proxy_url is forwarded from the standard proxy env vars; getproxies is
    patched so the result is platform-independent (it reads system config on
    macOS)."""

    def test_no_proxy_url_when_env_unset(self) -> None:
        with patch("urllib.request.getproxies", return_value={}):
            opts = obstore_client_options()
        assert "proxy_url" not in opts

    def test_https_proxy_is_used(self) -> None:
        with patch(
            "urllib.request.getproxies",
            return_value={"https": "http://proxy.corp:8080"},
        ):
            opts = obstore_client_options()
        assert opts["proxy_url"] == "http://proxy.corp:8080"

    def test_http_proxy_used_as_fallback(self) -> None:
        with patch(
            "urllib.request.getproxies",
            return_value={"http": "http://proxy.corp:3128"},
        ):
            opts = obstore_client_options()
        assert opts["proxy_url"] == "http://proxy.corp:3128"

    def test_https_preferred_over_http(self) -> None:
        with patch(
            "urllib.request.getproxies",
            return_value={
                "https": "http://secure.corp:8080",
                "http": "http://plain.corp:3128",
            },
        ):
            opts = obstore_client_options()
        assert opts["proxy_url"] == "http://secure.corp:8080"


# ---------------------------------------------------------------------------
# obstore_retry_config()
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_returns_none_when_no_env_set(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", raising=False)
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)
        assert obstore_retry_config() is None

    def test_max_retries_override(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", "3")

        cfg = obstore_retry_config()
        assert cfg == {"max_retries": 3}

    def test_retry_timeout_override(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", raising=False)
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", "600")

        cfg = obstore_retry_config()
        assert cfg == {"retry_timeout": timedelta(seconds=600)}

    def test_invalid_max_retries_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", "not-a-number")
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)
        cfg = obstore_retry_config()
        assert cfg is None  # silently falls back to upstream default

    def test_invalid_retry_timeout_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", raising=False)
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", "abc")
        cfg = obstore_retry_config()
        assert cfg is None


# ---------------------------------------------------------------------------
# log_obstore_config — observability anchor for the next RCA
# ---------------------------------------------------------------------------


class TestLogObstoreConfig:
    def test_logs_provider_and_options_at_info_level(self) -> None:
        with patch("application_sdk.storage._obstore_config.logger") as mock_logger:
            log_obstore_config(
                "s3",
                client_options={"timeout": "30m"},
                retry_config={"max_retries": 5},
            )
        mock_logger.info.assert_called_once()
        fmt, *args = mock_logger.info.call_args.args
        formatted = fmt % tuple(args)
        assert "s3" in formatted
        assert "30m" in formatted

    def test_logs_default_label_when_retry_config_is_none(self) -> None:
        with patch("application_sdk.storage._obstore_config.logger") as mock_logger:
            log_obstore_config("gcs", client_options={}, retry_config=None)
        # The default banner makes the next operator's RCA easier — they can
        # see the actual retry budget without grep'ing obstore source.
        mock_logger.info.assert_called_once()
        fmt, *args = mock_logger.info.call_args.args
        formatted = fmt % tuple(args)
        assert "default(max_retries=10" in formatted


# ---------------------------------------------------------------------------
# binding.py / cloud.py plumbing — the actual fix
# ---------------------------------------------------------------------------


class TestBindingPlumbsClientOptions:
    """``create_store_from_binding`` for ``s3`` must pass client_options +
    retry_config into ``S3Store``.  Without this the fix is purely cosmetic —
    the dict is built but never reaches obstore-rs.
    """

    def test_s3_binding_passes_client_options_and_retry_config(
        self, monkeypatch, tmp_path
    ) -> None:
        import yaml

        # Configure a non-default value so we can spot it on the call site.
        monkeypatch.setenv("ATLAN_OBSTORE_TIMEOUT", "42m")
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", "7")
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)

        components = tmp_path / "components"
        components.mkdir()
        component = {
            "apiVersion": "dapr.io/v1alpha1",
            "kind": "Component",
            "metadata": {"name": "objectstore"},
            "spec": {
                "type": "bindings.aws.s3",
                "metadata": [
                    {"name": "bucket", "value": "test-bucket"},
                    {"name": "region", "value": "us-east-1"},
                    {"name": "accessKey", "value": "AKIA..."},
                    {"name": "secretKey", "value": "secret"},
                ],
            },
        }
        (components / "objectstore.yaml").write_text(yaml.dump(component))

        with patch("obstore.store.S3Store") as mock_s3:
            mock_s3.return_value = MagicMock()
            from application_sdk.storage.binding import create_store_from_binding

            create_store_from_binding("objectstore", components_dir=components)

        assert mock_s3.called, "S3Store should have been instantiated"
        kwargs = mock_s3.call_args.kwargs
        client_opts = kwargs.get("client_options") or {}
        retry_cfg = kwargs.get("retry_config") or {}
        assert (
            client_opts.get("timeout") == "42m"
        ), f"timeout not plumbed into S3Store(client_options=...): {client_opts}"
        assert (
            retry_cfg.get("max_retries") == 7
        ), f"retry max_retries not plumbed into S3Store(retry_config=...): {retry_cfg}"


class TestCloudPlumbsClientOptions:
    """``CloudStore.from_credentials`` (external customer buckets) must
    receive the same plumbing — those flows hit *customer* infra over the
    public internet, exactly the path most likely to time out.
    """

    def test_external_s3_passes_client_options_and_retry_config(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_TIMEOUT", "42m")
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", "7")
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)

        with patch("obstore.store.S3Store") as mock_s3:
            mock_s3.return_value = MagicMock()
            from application_sdk.storage.cloud import CloudStore

            CloudStore.from_credentials(
                {
                    "authType": "s3",
                    "username": "AKIA...",
                    "password": "secret",
                    "extra": {"s3_bucket": "external-bucket", "region": "us-east-1"},
                }
            )
        assert mock_s3.called
        kwargs = mock_s3.call_args.kwargs
        client_opts = kwargs.get("client_options") or {}
        retry_cfg = kwargs.get("retry_config") or {}
        assert client_opts.get("timeout") == "42m"
        assert retry_cfg.get("max_retries") == 7
