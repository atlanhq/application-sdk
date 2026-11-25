"""Tests for obstore client_options and retry_config plumbing (BLDX-1155 #3).

A production RCA showed that ``S3Store`` was being created with nothing but
region+credentials — which means object_store-rs falls back to defaults sized
for small objects (request timeout 30 s).  These tests pin the env-var →
ClientConfig contract so large files do not silently inherit a small-object
timeout.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from application_sdk import _obstore_config
from application_sdk._obstore_config import (
    AzureStoreSettings,
    GCSStoreSettings,
    S3StoreSettings,
    _azure_config_has_explicit_credential,
    log_obstore_config,
    make_azure_store,
    obstore_client_options,
    obstore_retry_config,
)


@contextmanager
def _proxy_env(proxies: dict[str, str], **env: str) -> Iterator[None]:
    """Set ``<scheme>_proxy`` for each entry of *proxies* (``"no"`` is
    ``no_proxy``), with every other proxy variable unset; *env* sets more
    variables by name. pytest's monkeypatch restores only these variables."""
    with pytest.MonkeyPatch.context() as mp:
        for name in [n for n in os.environ if n.lower().endswith("_proxy")]:
            mp.delenv(name)
        for scheme, url in proxies.items():
            mp.setenv(f"{scheme}_proxy", url)
        for name, value in env.items():
            mp.setenv(name, value)
        yield


# ---------------------------------------------------------------------------
# obstore_client_options()
# ---------------------------------------------------------------------------


class TestClientOptionsDefaults:
    """SDK defaults are never empty — every store must inherit sane values."""

    def test_defaults_set_progress_and_backstop_timeouts(self, monkeypatch) -> None:
        # BLDX-1513: read_timeout (progress-based) is the primary liveness bound;
        # the overall-request timeout is a generous backstop (30m), not 90s —
        # a 90s overall cap killed slow-but-progressing GB-class downloads.
        for k in [
            "ATLAN_OBSTORE_TIMEOUT",
            "ATLAN_OBSTORE_READ_TIMEOUT",
            "ATLAN_OBSTORE_CONNECT_TIMEOUT",
            "ATLAN_OBSTORE_POOL_IDLE_TIMEOUT",
            "ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT",
            "ATLAN_OBSTORE_USER_AGENT",
            "ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST",
        ]:
            monkeypatch.delenv(k, raising=False)

        opts = obstore_client_options()
        assert opts["timeout"] == "30m"
        assert opts["read_timeout"] == "90s"
        assert opts["connect_timeout"] == "30s"
        assert opts["pool_idle_timeout"] == "90s"
        assert opts["http2_keep_alive_timeout"] == "30s"
        assert opts["user_agent"].startswith("atlan-application-sdk")

    def test_read_timeout_can_be_overridden_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_READ_TIMEOUT", "120s")
        opts = obstore_client_options()
        assert opts["read_timeout"] == "120s"

    def test_pool_max_idle_per_host_omitted_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST", raising=False)
        opts = obstore_client_options()
        assert "pool_max_idle_per_host" not in opts


class TestClientOptionsOverrides:
    def test_each_field_can_be_overridden_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_TIMEOUT", "1h")
        monkeypatch.setenv("ATLAN_OBSTORE_READ_TIMEOUT", "150s")
        monkeypatch.setenv("ATLAN_OBSTORE_CONNECT_TIMEOUT", "10s")
        monkeypatch.setenv("ATLAN_OBSTORE_POOL_IDLE_TIMEOUT", "120s")
        monkeypatch.setenv("ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT", "45s")
        monkeypatch.setenv("ATLAN_OBSTORE_USER_AGENT", "custom-agent/1.0")
        monkeypatch.setenv("ATLAN_OBSTORE_POOL_MAX_IDLE_PER_HOST", "32")

        # Pin no proxy and no custom CA so the exact-dict assertion is
        # deterministic regardless of the host's proxy env / SSL_CERT_DIR.
        with (
            _proxy_env({}),
            patch(
                "application_sdk.common.ssl_utils.get_custom_ca_cert_bytes",
                return_value=None,
            ),
        ):
            opts = obstore_client_options()
        assert opts == {
            "timeout": "1h",
            "read_timeout": "150s",
            "connect_timeout": "10s",
            "pool_idle_timeout": "120s",
            "http2_keep_alive_timeout": "45s",
            "user_agent": "custom-agent/1.0",
            "pool_max_idle_per_host": "32",
        }


_DURATION_ENV_DEFAULTS = {
    "ATLAN_OBSTORE_TIMEOUT": "30m",
    "ATLAN_OBSTORE_READ_TIMEOUT": "90s",
    "ATLAN_OBSTORE_CONNECT_TIMEOUT": "30s",
    "ATLAN_OBSTORE_POOL_IDLE_TIMEOUT": "90s",
    "ATLAN_OBSTORE_HTTP2_KEEP_ALIVE_TIMEOUT": "30s",
}


def _obstore_accepts_duration(value: str) -> bool:
    """Whether obstore itself parses *value* as a client timeout."""
    from obstore.exceptions import GenericError
    from obstore.store import S3Store

    try:
        S3Store(
            "probe-bucket",
            region="us-east-1",
            skip_signature=True,
            client_options={"timeout": value},
        )
    except GenericError:
        return False
    return True


class TestDurationEnvValues:
    """FND-5223: an empty or unparseable duration broke every store construction."""

    @pytest.mark.parametrize("key", sorted(_DURATION_ENV_DEFAULTS))
    def test_empty_value_is_the_default(self, monkeypatch, key: str) -> None:
        monkeypatch.setenv(key, "")
        opts = obstore_client_options()
        field = key.removeprefix("ATLAN_OBSTORE_").lower()
        assert opts[field] == _DURATION_ENV_DEFAULTS[key]

    def test_unparseable_value_is_the_default_and_logged(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_READ_TIMEOUT", "90")
        with patch.object(_obstore_config.logger, "warning") as warning:
            opts = obstore_client_options()
        assert opts["read_timeout"] == "90s"
        warning.assert_called_once()
        assert "ATLAN_OBSTORE_READ_TIMEOUT='90'" in warning.call_args.args[1]

    def test_empty_user_agent_is_the_default(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_OBSTORE_USER_AGENT", "")
        assert obstore_client_options()["user_agent"].startswith(
            "atlan-application-sdk/"
        )

    @pytest.mark.parametrize(
        "value",
        [
            *_DURATION_ENV_DEFAULTS.values(),
            "0",
            "0s",
            "1h 30m",
            "1h30m",
            "1.5h",
            "5 min",
            "500ms",
            "1µs",
            "1nsec",
            "3 wks",
            "2 hrs",
            "1M",
            "1y2M",
            "1h\t2m",
            "30",
            "s",
            "-1s",
            ".5s",
            "1.s",
            "1e3s",
            "+1s",
            "1H",
            "1Min",
            "1h,2m",
            "1msecs",
            "1mon",
            "1micros",
            "1h2",
            "forever",
            "\u0661s",
            "1.\u0665s",
            "\uff11s",
            "1h\u20032m",
            "\u00a030s",
        ],
    )
    def test_validator_agrees_with_obstore(self, value: str) -> None:
        """The SDK's duration check and obstore's parser give the same verdict.

        Differential, so an obstore upgrade that changes its grammar fails here
        instead of letting a value through that breaks every store construction.
        """
        from application_sdk._env import is_duration

        assert is_duration(value) == _obstore_accepts_duration(value)

    def test_empty_timeout_constructs_an_s3_store(self) -> None:
        """The reproduction from the issue, in a fresh interpreter.

        Before, ``make_s3_store`` raised ``Generic Config error: failed to parse
        "" as Duration`` whenever ``ATLAN_OBSTORE_TIMEOUT`` was set but empty.
        """
        import subprocess
        import sys

        code = (
            "from application_sdk._obstore_config import S3StoreSettings, "
            "make_s3_store\n"
            "make_s3_store(S3StoreSettings(bucket='probe-bucket', "
            "region='us-east-1', access_key_id='probe', "
            "secret_access_key='probe'))\n"
        )
        env = {**os.environ, "ATLAN_OBSTORE_TIMEOUT": ""}
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# proxy_url plumbing (obstore does not read proxy env vars itself)
# ---------------------------------------------------------------------------


class TestClientOptionsProxy:
    """proxy_url is the ``https`` proxy from ``_proxy``: HTTPS_PROXY, then
    ALL_PROXY, from the process environment only."""

    def test_no_proxy_url_when_env_unset(self) -> None:
        with _proxy_env({}):
            opts = obstore_client_options()
        assert "proxy_url" not in opts

    def test_https_proxy_is_used(self) -> None:
        with _proxy_env(
            {"https": "http://proxy.corp:8080"},
        ):
            opts = obstore_client_options()
        assert opts["proxy_url"] == "http://proxy.corp:8080"

    def test_http_proxy_is_not_used_for_https(self) -> None:
        # Object stores are reached over HTTPS; HTTP_PROXY applies to http only.
        with _proxy_env({"http": "http://proxy.corp:3128"}):
            opts = obstore_client_options()
        assert "proxy_url" not in opts

    def test_all_proxy_is_used(self) -> None:
        with _proxy_env({"all": "http://proxy.corp:3128"}):
            opts = obstore_client_options()
        assert opts["proxy_url"] == "http://proxy.corp:3128"

    def test_https_preferred_over_http(self) -> None:
        with _proxy_env(
            {
                "https": "http://secure.corp:8080",
                "http": "http://plain.corp:3128",
            },
        ):
            opts = obstore_client_options()
        assert opts["proxy_url"] == "http://secure.corp:8080"


class TestClientOptionsProxyExcludes:
    """NO_PROXY is forwarded as ``proxy_excludes`` — but only when a proxy URL is
    resolved (obstore ignores it otherwise)."""

    def test_no_proxy_excludes_forwarded_when_proxy_set(self) -> None:
        with _proxy_env(
            {
                "https": "http://proxy.corp:8080",
                "no": "localhost,.svc.cluster.local,10.0.0.0/8",
            }
        ):
            opts = obstore_client_options()
        assert opts["proxy_excludes"] == "localhost,.svc.cluster.local,10.0.0.0/8"

    def test_uppercase_no_proxy_is_honored(self) -> None:
        with _proxy_env(
            {"https": "http://proxy.corp:8080"}, NO_PROXY="internal.example.com"
        ):
            opts = obstore_client_options()
        assert opts["proxy_excludes"] == "internal.example.com"

    def test_excludes_omitted_without_proxy_url(self) -> None:
        # obstore only consults proxy_excludes when proxy_url is set, so don't
        # emit a dangling exclude list when there's no proxy.
        with _proxy_env({"no": "localhost"}):
            opts = obstore_client_options()
        assert "proxy_excludes" not in opts

    def test_excludes_omitted_when_no_proxy_unset(self) -> None:
        with _proxy_env({"https": "http://proxy.corp:8080"}):
            opts = obstore_client_options()
        assert "proxy_excludes" not in opts


# ---------------------------------------------------------------------------
# root_certificate plumbing (custom CA from SSL_CERT_DIR → obstore)
# ---------------------------------------------------------------------------


class TestClientOptionsRootCertificate:
    """SSL_CERT_DIR custom CAs are forwarded as obstore's ``root_certificate``."""

    def test_root_certificate_set_when_custom_ca_present(self) -> None:
        pem = b"-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"
        with (
            _proxy_env({}),
            patch(
                "application_sdk.common.ssl_utils.get_custom_ca_cert_bytes",
                return_value=pem,
            ),
        ):
            opts = obstore_client_options()
        assert opts["root_certificate"] == pem

    def test_root_certificate_omitted_without_custom_ca(self) -> None:
        with (
            _proxy_env({}),
            patch(
                "application_sdk.common.ssl_utils.get_custom_ca_cert_bytes",
                return_value=None,
            ),
        ):
            opts = obstore_client_options()
        assert "root_certificate" not in opts


# ---------------------------------------------------------------------------
# obstore_retry_config()
# ---------------------------------------------------------------------------


class TestRetryConfig:
    def test_documented_retry_env_vars_are_the_ones_read(self) -> None:
        """The module docstring names exactly the retry env vars the code reads.

        It once documented ``ATLAN_OBSTORE_RETRY_TIMEOUT`` while the code read
        ``ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS``, so an operator following the
        doc set a variable nothing reads.
        """
        import inspect
        import re

        documented = set(
            re.findall(r"``(ATLAN_OBSTORE_RETRY_\w+)``", _obstore_config.__doc__ or "")
        )
        read = set(
            re.findall(
                r'env_int\(\s*"(ATLAN_OBSTORE_RETRY_\w+)"',
                inspect.getsource(obstore_retry_config),
            )
        )
        assert read == {
            "ATLAN_OBSTORE_RETRY_MAX_RETRIES",
            "ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS",
        }
        assert documented == read

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

    @pytest.mark.parametrize(
        "key",
        ["ATLAN_OBSTORE_RETRY_MAX_RETRIES", "ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS"],
    )
    def test_negative_value_falls_back_to_default(self, monkeypatch, key) -> None:
        """A bare ``int()`` accepted ``-1`` and handed it to obstore."""
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", raising=False)
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)
        monkeypatch.setenv(key, "-1")
        with patch.object(_obstore_config.logger, "warning") as warning:
            assert obstore_retry_config() is None
        assert "is below 0" in warning.call_args.args[1]

    def test_zero_retries_is_kept(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_OBSTORE_RETRY_TIMEOUT_SECONDS", raising=False)
        monkeypatch.setenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", "0")
        assert obstore_retry_config() == {"max_retries": 0}


# ---------------------------------------------------------------------------
# log_obstore_config — observability anchor for the next RCA
# ---------------------------------------------------------------------------


class TestLogObstoreConfig:
    def test_logs_provider_and_options_at_info_level(self) -> None:
        with patch("application_sdk._obstore_config.logger") as mock_logger:
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
        with patch("application_sdk._obstore_config.logger") as mock_logger:
            log_obstore_config("gcs", client_options={}, retry_config=None)
        # The default banner makes the next operator's RCA easier — they can
        # see the actual retry budget without grep'ing obstore source.
        mock_logger.info.assert_called_once()
        fmt, *args = mock_logger.info.call_args.args
        formatted = fmt % tuple(args)
        assert "default(max_retries=10" in formatted

    def test_redacts_proxy_userinfo(self) -> None:
        """A proxy_url with credentials must not log the password."""
        with patch("application_sdk._obstore_config.logger") as mock_logger:
            log_obstore_config(
                "s3",
                client_options={"proxy_url": "http://user:secretpass@proxy.corp:8080"},
                retry_config=None,
            )
        fmt, *args = mock_logger.info.call_args.args
        formatted = fmt % tuple(args)
        assert "secretpass" not in formatted
        assert "***@proxy.corp:8080" in formatted

    def test_summarizes_root_certificate_bytes(self) -> None:
        """The full PEM bundle is noise — the log shows only a byte-count."""
        pem = (
            b"-----BEGIN CERTIFICATE-----\nsecretlookingcert\n-----END CERTIFICATE-----"
        )
        with patch("application_sdk._obstore_config.logger") as mock_logger:
            log_obstore_config(
                "s3",
                client_options={"root_certificate": pem},
                retry_config=None,
            )
        fmt, *args = mock_logger.info.call_args.args
        formatted = fmt % tuple(args)
        assert "secretlookingcert" not in formatted
        assert f"<{len(pem)} bytes>" in formatted


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
        assert client_opts.get("timeout") == "42m", (
            f"timeout not plumbed into S3Store(client_options=...): {client_opts}"
        )
        assert retry_cfg.get("max_retries") == 7, (
            f"retry max_retries not plumbed into S3Store(retry_config=...): {retry_cfg}"
        )


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


# ---------------------------------------------------------------------------
# make_azure_store — ambient AZURE_* env isolation
# ---------------------------------------------------------------------------


def _azure_settings(config: dict[str, str]) -> AzureStoreSettings:
    """Settings whose obstore config is *config* (``azure_storage_<field>`` keys)."""
    fields = {k.removeprefix("azure_storage_"): v for k, v in config.items()}
    return AzureStoreSettings(container="container", **fields)


class TestAzureAmbientEnvIsolation:
    """obstore reads ``AZURE_*`` from the environment at construction and lets a
    host-injected value outrank the caller's explicit credential. ``make_azure_store``
    must strip the ambient env while building from an explicit credential, and leave it
    in place for the managed/workload-identity case.
    """

    def _build_and_capture(self, config: dict | None) -> dict:
        """Build via make_azure_store with ``obstore.store.AzureStore`` stubbed; return
        the AZURE_* env visible *during* construction plus the kwargs passed to obstore."""
        captured: dict = {}

        def fake_azure_store(**kw):
            captured["azure_env"] = {
                k: v for k, v in os.environ.items() if k.startswith("AZURE_")
            }
            captured["kw"] = kw
            return MagicMock(name="AzureStore")

        with patch("obstore.store.AzureStore", new=fake_azure_store):
            make_azure_store(_azure_settings(config))
        return captured

    def test_explicit_account_key_strips_ambient_env_during_build(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCESS_KEY", "host-key")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/token")
        monkeypatch.setenv("AZURE_CLIENT_ID", "host-client")
        cap = self._build_and_capture(
            {
                "azure_storage_account_name": "acct",
                "azure_storage_account_key": "cust-key",
            }
        )
        # no ambient identity leaks into the construction — only our config is used
        assert cap["azure_env"] == {}
        assert cap["kw"]["config"]["azure_storage_account_key"] == "cust-key"
        # ambient env restored afterwards
        assert os.environ["AZURE_STORAGE_ACCESS_KEY"] == "host-key"
        assert os.environ["AZURE_FEDERATED_TOKEN_FILE"] == "/var/run/token"
        assert os.environ["AZURE_CLIENT_ID"] == "host-client"

    def test_explicit_client_secret_strips_ambient_env(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCESS_KEY", "host-key")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/token")
        cap = self._build_and_capture(
            {
                "azure_storage_account_name": "acct",
                "azure_storage_client_id": "c",
                "azure_storage_tenant_id": "t",
                "azure_storage_client_secret": "s",
            }
        )
        assert cap["azure_env"] == {}
        assert os.environ["AZURE_STORAGE_ACCESS_KEY"] == "host-key"

    def test_no_credential_preserves_ambient_env(self, monkeypatch):
        # Managed / workload identity: the caller passed no standalone credential, so
        # the host's ambient identity must stay visible to obstore.
        monkeypatch.setenv("AZURE_STORAGE_ACCESS_KEY", "host-key")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/token")
        cap = self._build_and_capture({"azure_storage_account_name": "acct"})
        assert cap["azure_env"]["AZURE_STORAGE_ACCESS_KEY"] == "host-key"
        assert cap["azure_env"]["AZURE_FEDERATED_TOKEN_FILE"] == "/var/run/token"

    def test_ambient_env_restored_even_on_construction_error(self, monkeypatch):
        monkeypatch.setenv("AZURE_STORAGE_ACCESS_KEY", "host-key")

        def boom(**kw):
            raise RuntimeError("construction failed")

        with patch("obstore.store.AzureStore", new=boom):
            with pytest.raises(RuntimeError, match="construction failed"):
                make_azure_store(
                    AzureStoreSettings(
                        container="container", account_name="a", account_key="k"
                    )
                )
        assert os.environ["AZURE_STORAGE_ACCESS_KEY"] == "host-key"

    @pytest.mark.parametrize(
        "config, expected",
        [
            ({"azure_storage_account_key": "k"}, True),
            ({"azure_storage_client_secret": "s"}, True),
            ({"azure_storage_sas_key": "sas"}, True),
            ({"azure_storage_token": "tok"}, True),
            ({"azure_storage_account_name": "a"}, False),  # managed identity
            # SP without a secret → relies on workload identity, keep ambient env
            ({"azure_storage_client_id": "c", "azure_storage_tenant_id": "t"}, False),
            (
                {"azure_storage_account_key": ""},
                False,
            ),  # empty value is not a credential
            ({}, False),
            (None, False),
        ],
    )
    def test_explicit_credential_gate(self, config, expected):
        assert _azure_config_has_explicit_credential(config) is expected

    def test_credential_provider_strips_ambient_env(self, monkeypatch):
        # cert-auth and other credential_provider paths leave config without any
        # of the four guarded keys, so the original gate (_azure_config_has_explicit_credential)
        # returned False and ambient AZURE_* env was NOT stripped — the same host-identity
        # outranking bug as the account-key case.  The gate must also fire when
        # credential_provider is not None.
        monkeypatch.setenv("AZURE_STORAGE_ACCESS_KEY", "host-key")
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", "/var/run/token")
        monkeypatch.setenv("AZURE_CLIENT_ID", "host-client")

        captured: dict = {}

        def fake_azure_store(**kw):
            captured["azure_env"] = {
                k: v for k, v in os.environ.items() if k.startswith("AZURE_")
            }
            captured["kw"] = kw
            return MagicMock(name="AzureStore")

        fake_provider = MagicMock(name="cert-credential-provider")
        with patch("obstore.store.AzureStore", new=fake_azure_store):
            make_azure_store(
                AzureStoreSettings(
                    container="container",
                    account_name="acct",
                    credential_provider=fake_provider,
                )
            )

        assert captured["azure_env"] == {}, (
            "ambient AZURE_* env must be stripped when credential_provider is supplied"
        )
        assert captured["kw"]["credential_provider"] is fake_provider
        # env restored after construction
        assert os.environ["AZURE_STORAGE_ACCESS_KEY"] == "host-key"
        assert os.environ["AZURE_FEDERATED_TOKEN_FILE"] == "/var/run/token"
        assert os.environ["AZURE_CLIENT_ID"] == "host-client"


# ---------------------------------------------------------------------------
# Config passthrough — regression guard for binding.py / cloud.py
#
# These tests verify that make_s3_store / make_gcs_store forward the caller's
# config dict to the obstore constructor without dropping or mutating keys.
# The Rust-layer credential precedence (explicit config vs. ambient env vars)
# was confirmed by live requests during the PR audit but cannot be exercised
# at the unit level — integration tests cover that invariant.
# ---------------------------------------------------------------------------


class TestS3ConfigPassthrough:
    """Regression guard: the config dict reaches S3Store unchanged.

    These tests assert that make_s3_store passes the caller's config dict to the
    S3Store constructor without dropping or mutating keys.  They do NOT verify the
    Rust-layer credential precedence (explicit config vs. ambient env vars) — that
    behaviour is tested by integration tests against a real or mocked S3 endpoint.
    """

    def test_full_explicit_credentials_reach_s3_store(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENV-ACCESS-KEY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENV-SECRET-KEY")

        with patch("obstore.store.S3Store") as mock_s3:
            mock_s3.return_value = MagicMock()
            from application_sdk._obstore_config import make_s3_store

            make_s3_store(
                S3StoreSettings(
                    bucket="test-bucket",
                    access_key_id="CONFIG-ACCESS-KEY",
                    secret_access_key="CONFIG-SECRET-KEY",
                    region="us-east-1",
                )
            )

        config = mock_s3.call_args.kwargs["config"]
        assert config["aws_access_key_id"] == "CONFIG-ACCESS-KEY"
        assert config["aws_secret_access_key"] == "CONFIG-SECRET-KEY"

    def test_no_credentials_in_config_passes_none(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ENV-ACCESS-KEY")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ENV-SECRET-KEY")

        with patch("obstore.store.S3Store") as mock_s3:
            mock_s3.return_value = MagicMock()
            from application_sdk._obstore_config import make_s3_store

            make_s3_store(S3StoreSettings(bucket="test-bucket"))

        assert mock_s3.call_args.kwargs["config"] is None


class TestGCSConfigPassthrough:
    """Regression guard: the config dict reaches GCSStore unchanged.

    These tests assert that make_gcs_store passes the caller's config dict to the
    GCSStore constructor without dropping or mutating keys.  They do NOT verify the
    Rust-layer credential precedence (SA key in config vs. ADC / Workload Identity) —
    that behaviour is tested by integration tests against a real or mocked GCS endpoint.
    """

    def test_explicit_sa_key_reaches_gcs_store(self, monkeypatch) -> None:
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/adc.json")

        with patch("obstore.store.GCSStore") as mock_gcs:
            mock_gcs.return_value = MagicMock()
            from application_sdk._obstore_config import make_gcs_store

            make_gcs_store(
                GCSStoreSettings(
                    bucket="test-bucket",
                    service_account_key='{"type":"service_account"}',
                )
            )

        config = mock_gcs.call_args.kwargs["config"]
        assert config is not None
        assert "google_service_account_key" in config

    def test_no_sa_key_passes_none_config_for_adc(self, monkeypatch) -> None:
        # No SA key → config=None → obstore uses ADC / Workload Identity.
        # Verified empirically: GCSStore(bucket=...) with no config and
        # GOOGLE_APPLICATION_CREDENTIALS pointing to a nonexistent path fails
        # at construction with "No such file or directory" — proving ADC is only
        # consulted when the config SA key is absent.
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/adc.json")

        with patch("obstore.store.GCSStore") as mock_gcs:
            mock_gcs.return_value = MagicMock()
            from application_sdk._obstore_config import make_gcs_store

            make_gcs_store(GCSStoreSettings(bucket="test-bucket"))

        assert mock_gcs.call_args.kwargs["config"] is None


class TestStoreSettingsRepr:
    """A logged settings object must not carry a credential."""

    def test_s3_settings_repr_hides_credentials(self) -> None:
        text = repr(
            S3StoreSettings(
                bucket="b",
                access_key_id="AKID-EXAMPLE",
                secret_access_key="secret-example",
                session_token="token-example",
            )
        )
        assert "b" in text
        for secret in ("AKID-EXAMPLE", "secret-example", "token-example"):
            assert secret not in text

    def test_azure_and_gcs_settings_repr_hide_credentials(self) -> None:
        az = repr(
            AzureStoreSettings(
                container="c",
                account_key="key-example",
                sas_key="sas-example",
                client_secret="cs-example",
            )
        )
        gcs = repr(GCSStoreSettings(bucket="b", service_account_key="sa-example"))
        for secret in ("key-example", "sas-example", "cs-example"):
            assert secret not in az
        assert "sa-example" not in gcs
