"""Unit tests for the DataForge rung-3 e2e integration (offline, fake transport)."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from application_sdk.testing.e2e.dataforge import (
    DataForgeAuthError,
    DataForgeClient,
    DataForgeError,
    creds_to_database_spec,
    load_seed,
)

_ARTIFACTS = {
    "host": "redshift.example.invalid",
    "port": 5439,
    "database": "e2e_main",
    "username": "test_user",
    "password": "test-not-a-real-pw",
}


_API = "https://dataforge.example.invalid"


class FakeTransport:
    """Scripted (method, path-suffix) → (status, json) transport recorder."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, headers, body):
        sp = urlsplit(url)
        path = sp.path + (f"?{sp.query}" if sp.query else "")
        self.calls.append((method, path, json.loads(body) if body else None))
        for (m, suffix), resp in self.routes.items():
            if m == method and path.startswith(suffix):
                return resp
        return 404, {"error": f"no route for {method} {path}"}


def _client(transport: FakeTransport) -> DataForgeClient:
    # _token preset → skips the session-file load entirely.
    return DataForgeClient(transport=transport, _token="fake-session", api_url=_API)


class TestSession:
    def test_inline_session_env_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("DATAFORGE_SESSION", json.dumps({"session_token": "tok"}))
        c = DataForgeClient(transport=FakeTransport({}), api_url=_API)
        assert c._headers()["Authorization"] == "Bearer tok"

    def test_missing_session_raises_auth_error(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("DATAFORGE_SESSION", raising=False)
        monkeypatch.setenv("DATAFORGE_SESSION_FILE", str(tmp_path / "nope.json"))
        c = DataForgeClient(transport=FakeTransport({}), api_url=_API)
        with pytest.raises(DataForgeAuthError, match="No DataForge session"):
            c._headers()

    def test_401_surfaces_auth_error(self) -> None:
        t = FakeTransport({("GET", "/api/v1/resources"): (401, {"code": "EXPIRED"})})
        with pytest.raises(DataForgeAuthError, match="session expired"):
            _client(t).find_reusable("redshift")


class TestLifecycle:
    def test_resolve_module_id_picks_variant(self) -> None:
        t = FakeTransport(
            {
                ("GET", "/api/v1/modules/datasources/redshift/variants"): (
                    200,
                    [
                        {"variant": "aws-managed", "module_id": "m-managed"},
                        {"variant": "aws-serverless", "module_id": "m-serverless"},
                    ],
                )
            }
        )
        assert (
            _client(t).resolve_module_id("redshift", "aws-serverless") == "m-serverless"
        )

    def test_resolve_module_id_unknown_variant_raises(self) -> None:
        t = FakeTransport(
            {
                ("GET", "/api/v1/modules/datasources/redshift/variants"): (
                    200,
                    [{"variant": "aws-managed", "module_id": "m"}],
                )
            }
        )
        with pytest.raises(DataForgeError, match="No DataForge variant"):
            _client(t).resolve_module_id("redshift", "aws-serverless")

    def test_find_reusable_returns_provisioned(self) -> None:
        t = FakeTransport(
            {
                ("GET", "/api/v1/resources"): (
                    200,
                    [
                        {"id": "r-old", "status": "DELETED"},
                        {"id": "r-live", "status": "PROVISIONED"},
                    ],
                )
            }
        )
        assert _client(t).find_reusable("redshift") == "r-live"

    def test_create_rejects_short_reason(self) -> None:
        with pytest.raises(DataForgeError, match="reason"):
            _client(FakeTransport({})).create("m", {"instance_name": "x"}, "short")

    def test_create_defaults_to_persistent_no_ttl(self) -> None:
        # Default posture is the persistent shared fixture: lifecycle_enabled=False
        # and NO lifecycle_days key (no hard TTL — approve once, reuse forever).
        t = FakeTransport({("POST", "/api/v1/resources"): (201, {"id": "r-new"})})
        rid = _client(t).create(
            "m-1", {"instance_name": "e2e"}, "SDR full-DAG e2e for redshift"
        )
        assert rid == "r-new"
        body = t.calls[0][2]
        assert body["skip_approval"] is True
        assert body["category"] == "development"
        assert body["lifecycle_enabled"] is False
        assert "lifecycle_days" not in body
        assert body["module_id"] == "m-1"

    def test_create_ephemeral_opt_in_sets_ttl(self) -> None:
        # Explicit throwaway instance: lifecycle_enabled=True carries a hard TTL.
        t = FakeTransport({("POST", "/api/v1/resources"): (201, {"id": "r-tmp"})})
        _client(t).create(
            "m-1",
            {"instance_name": "e2e"},
            "throwaway ephemeral e2e instance",
            lifecycle_enabled=True,
            lifecycle_days=3,
        )
        body = t.calls[0][2]
        assert body["lifecycle_enabled"] is True
        assert body["lifecycle_days"] == 3

    def test_poll_returns_on_provisioned(self) -> None:
        seq = [
            (200, {"id": "r", "status": "PROVISIONING"}),
            (
                200,
                {"id": "r", "status": "PROVISIONED", "artifacts": {"data": _ARTIFACTS}},
            ),
        ]

        def transport(method, url, headers, body):
            return seq.pop(0)

        client = DataForgeClient(
            transport=transport, _token="fake-session", api_url=_API
        )
        got = client.poll("r", interval_s=0, sleep=lambda _s: None)
        assert got["status"] == "PROVISIONED"

    def test_poll_raises_on_failed(self) -> None:
        t = FakeTransport({("GET", "/api/v1/resources/r"): (200, {"status": "FAILED"})})
        with pytest.raises(DataForgeError, match="FAILED"):
            _client(t).poll("r", interval_s=0, sleep=lambda _s: None)

    def test_poll_times_out(self) -> None:
        t = FakeTransport(
            {("GET", "/api/v1/resources/r"): (200, {"status": "PROVISIONING"})}
        )
        clock = iter([0.0, 0.0, 999.0])
        with pytest.raises(DataForgeError, match="not PROVISIONED within"):
            _client(t).poll(
                "r",
                timeout_s=10,
                interval_s=0,
                sleep=lambda _s: None,
                now=lambda: next(clock),
            )

    def test_provision_or_reuse_reuses(self) -> None:
        t = FakeTransport(
            {
                ("GET", "/api/v1/resources/r-live"): (
                    200,
                    {
                        "id": "r-live",
                        "status": "PROVISIONED",
                        "artifacts": {"data": _ARTIFACTS},
                    },
                ),
                ("GET", "/api/v1/resources"): (
                    200,
                    [{"id": "r-live", "status": "PROVISIONED"}],
                ),
            }
        )
        rid, creds = _client(t).provision_or_reuse(
            datasource="redshift",
            variant="aws-serverless",
            instance_name="e2e",
            reason="SDR full-DAG e2e for redshift",
        )
        assert rid == "r-live"
        assert creds["database"] == "e2e_main"
        # reuse path must NOT create a new resource
        assert not any(m == "POST" for m, _p, _b in t.calls)


class TestMapping:
    def test_creds_to_database_spec_puts_database_in_extra(self) -> None:
        spec = creds_to_database_spec(
            _ARTIFACTS, connector_config_name="atlan-connectors-redshift"
        )
        assert spec.host == "redshift.example.invalid"
        assert spec.port == 5439
        assert spec.username == "test_user"
        assert spec.extra == {"database": "e2e_main"}
        assert spec.connector_config_name == "atlan-connectors-redshift"

    def test_creds_alt_key_spellings(self) -> None:
        spec = creds_to_database_spec(
            {
                "endpoint": "h",
                "port": "5439",
                "dbname": "e2e_main",
                "user": "u",
                "password": "p",
            }
        )
        assert spec.host == "h" and spec.port == 5439
        assert spec.username == "u" and spec.extra == {"database": "e2e_main"}

    def test_missing_artifacts_raises(self) -> None:
        from application_sdk.testing.e2e.dataforge import _artifacts

        with pytest.raises(DataForgeError, match="artifacts.data"):
            _artifacts({"id": "r", "artifacts": {}})


class TestSeed:
    def test_redshift_seed_shape(self) -> None:
        sql = load_seed("redshift")
        assert "CREATE SCHEMA IF NOT EXISTS e2e_main" in sql
        assert "e2e_main.customers" in sql and "e2e_main.orders" in sql
        assert "v_customer_order_totals" in sql
        # Redshift dialect in the DDL (not the postgres SERIAL form)
        assert "INTEGER IDENTITY(1,1) PRIMARY KEY" in sql
        assert "SERIAL PRIMARY KEY" not in sql

    def test_unknown_engine_seed_raises(self) -> None:
        with pytest.raises(DataForgeError, match="no canonical seed"):
            load_seed("snowflake")


class TestCli:
    from application_sdk.testing.e2e.dataforge import main, provision_source

    def _reuse_transport(self) -> FakeTransport:
        return FakeTransport(
            {
                ("GET", "/api/v1/resources/r-live"): (
                    200,
                    {
                        "id": "r-live",
                        "status": "PROVISIONED",
                        "artifacts": {"data": _ARTIFACTS},
                    },
                ),
                ("GET", "/api/v1/resources"): (
                    200,
                    [{"id": "r-live", "status": "PROVISIONED"}],
                ),
            }
        )

    def test_provision_emits_source_env(self, monkeypatch, tmp_path) -> None:
        from application_sdk.testing.e2e.dataforge import main

        env_file = tmp_path / "gh.env"
        monkeypatch.setenv("GITHUB_ENV", str(env_file))
        rc = main(
            [
                "provision",
                "redshift",
                "--instance-name",
                "e2e",
                "--reason",
                "SDR full-DAG e2e for redshift",
            ],
            client=_client(self._reuse_transport()),
        )
        assert rc == 0
        written = env_file.read_text()
        assert "SOURCE_HOST=redshift.example.invalid" in written
        assert "SOURCE_DATABASE=e2e_main" in written
        assert "SOURCE_USERNAME=test_user" in written
        assert "DATAFORGE_RESOURCE_ID=r-live" in written
        # SECURITY: the password must NEVER be written to $GITHUB_ENV
        assert "SOURCE_PASSWORD" not in written
        assert "test-not-a-real-pw" not in written

    def test_verify_ok(self) -> None:
        from application_sdk.testing.e2e.dataforge import main

        t = FakeTransport({("GET", "/api/v1/modules"): (200, {"modules": []})})
        assert main(["verify"], client=_client(t)) == 0

    def test_provision_source_unknown_engine(self) -> None:
        from application_sdk.testing.e2e.dataforge import provision_source

        with pytest.raises(DataForgeError, match="not registered for DataForge"):
            provision_source("snowflake", instance_name="x", reason="x" * 12)


class TestDeviceLogin:
    def test_device_login_polls_then_writes_session(self, tmp_path) -> None:
        session_path = tmp_path / ".dataforge" / "session.json"
        responses = [
            (
                200,
                {
                    "device_code": "dc-1",
                    "user_code": "ABCD-EFGH",
                    "verification_url_complete": "https://dataforge.example.invalid/auth/device?user_code=ABCD-EFGH",
                    "interval": 0,
                    "expires_in": 600,
                },
            ),
            (400, {"error": "authorization_pending"}),
            (200, {"session_token": "test-session", "refresh_token": "test-refresh"}),
        ]

        def transport(method, url, headers, body):
            return responses.pop(0)

        client = DataForgeClient(transport=transport, api_url=_API)
        clock = iter([0.0, 0.0, 0.0, 0.0])
        prompts: list[str] = []
        out = client.device_login(
            open_browser=False,
            write_path=str(session_path),
            sleep=lambda _s: None,
            now=lambda: next(clock),
            echo=prompts.append,
        )
        assert out == str(session_path)
        saved = json.loads(session_path.read_text())
        assert saved["session_token"] == "test-session"
        assert saved["refresh_token"] == "test-refresh"
        assert (session_path.stat().st_mode & 0o777) == 0o600
        # the human-facing prompt carried the verification URL + code
        assert any("ABCD-EFGH" in p for p in prompts)

    def test_device_login_raises_on_access_denied(self, tmp_path) -> None:
        responses = [
            (
                200,
                {"device_code": "dc", "user_code": "X", "interval": 0, "expires_in": 9},
            ),
            (400, {"error": "access_denied"}),
        ]

        def transport(method, url, headers, body):
            return responses.pop(0)

        clock = iter([0.0, 0.0, 0.0])
        with pytest.raises(DataForgeError, match="access_denied"):
            DataForgeClient(transport=transport, api_url=_API).device_login(
                open_browser=False,
                write_path=str(tmp_path / "s.json"),
                sleep=lambda _s: None,
                now=lambda: next(clock),
                echo=lambda _m: None,
            )


class TestRefresh:
    def test_401_then_refresh_then_retry_succeeds(self) -> None:
        # GET 401 (expired) -> POST /auth/refresh 200 -> GET retry 200.
        seq = [
            (401, {"error": "unauthorized"}),
            (200, {"session_token": "fresh-session", "refresh_token": "fresh-refresh"}),
            (200, [{"id": "r-live", "status": "PROVISIONED"}]),
        ]
        seen = []

        def transport(method, url, headers, body):
            seen.append((method, url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1]))
            return seq.pop(0)

        c = DataForgeClient(
            transport=transport,
            api_url=_API,
            _token="stale-session",
            _refresh_token="rt",
        )
        assert c.find_reusable("redshift") == "r-live"
        assert c._token == "fresh-session"  # rotated
        # the middle call hit the refresh endpoint
        assert any("auth/refresh" in p for _m, p in seen)

    def test_401_without_refresh_token_raises(self) -> None:
        t = FakeTransport({("GET", "/api/v1/resources"): (401, {"error": "x"})})
        # _client() sets no refresh token -> refresh() returns False -> raises
        with pytest.raises(DataForgeAuthError, match="session expired"):
            _client(t).find_reusable("redshift")
