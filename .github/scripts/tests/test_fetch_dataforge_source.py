"""Tests for fetch_dataforge_source.py — resolution, normalization, emission.

The script's security contract is split: IT only fetches and re-shapes,
printing a JSON env map to stdout; masking and ``$GITHUB_ENV`` writes belong
to export_extra_env.py's audited two-pass call sites. So these tests cover the
resolution chains (resource / managed), the field normalization, and the
stdout/stderr split — and pin the workflow/action call-site shape that keeps
credential values out of the log (command substitution feeding the two-pass
exporter).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS_DIR))

import fetch_dataforge_source as fds

_REPO_ROOT = _SCRIPTS_DIR.parents[1]

# ── Fixtures ──────────────────────────────────────────────────────────────────

_POSTGRES_DATA = {
    "type": "postgres",
    "provider": "aws",
    "host": "db.example.internal",
    "port": 5432,
    "database": "dataforge",
    "username": "df_user",
    "password": "s3cret",
    "jdbc_url": "jdbc:postgresql://db.example.internal:5432/dataforge",
    "iam": {"enabled": False},
    "metadata": {"request_id": "r-1"},
}


def _resource_doc(status="PROVISIONED", data=_POSTGRES_DATA):
    return {"id": "res-1", "status": status, "artifacts": {"data": data}}


# ── Export map (verbatim pass-through) ───────────────────────────────────────


def test_exports_every_raw_scalar_verbatim_prefixed():
    out = fds.build_exports(_POSTGRES_DATA, "postgres")
    # Field names come straight from dataforge — no interpretation.
    assert out["E2E_POSTGRES_HOST"] == "db.example.internal"
    assert out["E2E_POSTGRES_PORT"] == "5432"
    assert out["E2E_POSTGRES_DATABASE"] == "dataforge"
    assert out["E2E_POSTGRES_USERNAME"] == "df_user"
    assert out["E2E_POSTGRES_PASSWORD"] == "s3cret"
    assert out["E2E_POSTGRES_JDBC_URL"].startswith("jdbc:postgresql://")
    # No canonical shape is imposed.
    assert not any(k.startswith("E2E_SOURCE_HOST") for k in out)


def test_non_basic_auth_shapes_pass_through_unmangled():
    """IAM-role/key-pair/OAuth sources have no username/password to fake."""
    raw = {
        "iam_role_arn": "arn:aws:iam::1:role/e2e",
        "external_id": "ext-1",
        "region": "us-east-1",
        "host": "db.internal",
        "database": "app",
    }
    out = fds.build_exports(raw, "postgres")
    assert out["E2E_POSTGRES_IAM_ROLE_ARN"] == "arn:aws:iam::1:role/e2e"
    assert out["E2E_POSTGRES_EXTERNAL_ID"] == "ext-1"
    assert "E2E_POSTGRES_PASSWORD" not in out  # nothing invented


def test_saas_shape_passes_through():
    raw = {
        "tenant_id": "t-1",
        "client_id": "c-1",
        "client_secret": "cs-1",
        "workspace_id": "w-1",
    }
    out = fds.build_exports(raw, "powerbi")
    assert out["E2E_POWERBI_TENANT_ID"] == "t-1"
    assert out["E2E_POWERBI_CLIENT_SECRET"] == "cs-1"


def test_nested_values_go_to_extra_json_and_raw_holds_scalars():
    out = fds.build_exports(_POSTGRES_DATA, "postgres")
    extra = json.loads(out["E2E_SOURCE_EXTRA_JSON"])
    assert extra == {"iam": {"enabled": False}, "metadata": {"request_id": "r-1"}}
    raw = json.loads(out["E2E_SOURCE_RAW_JSON"])
    assert raw["password"] == "s3cret"
    assert "iam" not in raw


def test_env_names_are_sanitized_and_output_prefix_respected():
    out = fds.build_exports({"https-url": "https://x"}, "metabase", "META_BASE")
    assert out["E2E_META_BASE_HTTPS_URL"] == "https://x"


def test_datasource_breadcrumb_is_exported():
    """tests-reusable's source-selection notice keys off this."""
    out = fds.build_exports(_POSTGRES_DATA, "postgres")
    assert out["E2E_SOURCE_DATASOURCE"] == "postgres"


def test_all_values_are_strings():
    """export_extra_env.parse rejects non-scalars — the hand-off must be safe."""
    out = fds.build_exports(_POSTGRES_DATA, "postgres")
    assert all(isinstance(v, str) for v in out.values())


# ── HTTP error hardening (the error body is never echoed) ────────────────────


class _FakeHTTPError(fds.urllib.error.HTTPError):
    """HTTPError with a controllable body (the real one reads from a socket)."""

    def __init__(self, code, body: bytes):
        super().__init__("https://b/api", code, "msg", None, None)
        self._body = body

    def read(self, *args, **kwargs):
        return self._body


def _http_error(code, body: bytes):
    return _FakeHTTPError(code, body)


def test_error_body_credential_value_never_reaches_message(monkeypatch):
    """A credential-shaped value embedded in a dataforge error body must NOT
    appear in the raised error (which is printed to stderr before any masking
    exists)."""
    secret = "df_live_secretvalue_123"
    body = json.dumps({"error": "vault_item_missing", "detail": secret}).encode()
    monkeypatch.setattr(
        fds.urllib.request,
        "urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(_http_error(404, body)),
    )
    with pytest.raises(fds.DataforgeSourceError) as excinfo:
        fds._http_get("https://b/api/v1/resources/res-1", "k")
    msg = str(excinfo.value)
    assert secret not in msg
    assert "404" in msg
    assert "vault_item_missing" in msg  # allowlisted failure class survives


def test_error_body_unrecognized_class_collapses_to_request_failed(monkeypatch):
    """A non-allowlisted error field — or one carrying a value — collapses to
    the generic class, so nothing body-derived reaches stderr."""
    secret = "p@ssw0rd-value"
    body = json.dumps({"error": secret}).encode()
    monkeypatch.setattr(
        fds.urllib.request,
        "urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(_http_error(500, body)),
    )
    with pytest.raises(fds.DataforgeSourceError) as excinfo:
        fds._http_get("https://b/api/v1/resources/res-1", "k")
    msg = str(excinfo.value)
    assert secret not in msg
    assert "request_failed" in msg


def test_error_body_non_json_yields_generic_class(monkeypatch):
    monkeypatch.setattr(
        fds.urllib.request,
        "urlopen",
        lambda req, timeout=0: (_ for _ in ()).throw(
            _http_error(502, b"<html>bad gateway</html>")
        ),
    )
    with pytest.raises(fds.DataforgeSourceError, match="request_failed"):
        fds._http_get("https://b/api/v1/resources/res-1", "k")


# ── Resolution: resource mode ─────────────────────────────────────────────────


def test_main_without_oidc_env_fails_before_any_request(monkeypatch, capsys):
    """No runner OIDC endpoint -> actionable error, zero network calls."""
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)

    def boom(*a, **k):  # any HTTP call would be a bug
        raise AssertionError("no request may be made without OIDC env")

    monkeypatch.setattr(fds, "_http_get", boom)
    assert fds.main(["--datasource", "postgres", "--resource-id", "r"]) == 1
    assert "id-token" in capsys.readouterr().err


def test_main_stdout_is_pure_json(monkeypatch, capsys):
    """stdout must be command-substitution-safe: one JSON object, nothing else."""
    monkeypatch.setattr(
        fds, "_github_oidc_token", lambda audience="dataforge": "oidc-jwt"
    )
    monkeypatch.setattr(fds, "_exchange_for_service_token", lambda b, o: "svc")
    monkeypatch.setattr(
        fds,
        "resolve_via_endpoint",
        lambda *a, **k: (
            {
                "host": "db.example.internal",
                "username": "u",
                "password": "s3cret",
                "database": "d",
            },
            "res-1",
        ),
    )

    assert fds.main(["--datasource", "postgres", "--resource-id", "res-1"]) == 0
    captured = capsys.readouterr()
    exports = json.loads(captured.out)  # would raise if stdout carried extras
    assert exports["E2E_POSTGRES_HOST"] == "db.example.internal"
    # Breadcrumbs go to stderr and never include values.
    assert "Resolved dataforge source" in captured.err
    assert "s3cret" not in captured.err


# ── Call-site shape guards ────────────────────────────────────────────────────
# The credential-safety property lives at the call sites: stdout captured into
# a shell variable (never the log), then export_extra_env.py's mask-first
# two-pass — whose ordering is audited by test_export_extra_env.py's discovered
# call-site guard. These pin the capture shape itself, which that guard cannot
# see (it audits export_extra_env invocations, not what fed them).

_CAPTURE_SHAPE = re.compile(
    r"DF_ENV_JSON=\"\$\((?:python3|python)[^\n]*fetch_dataforge_source\.py"
)


def test_tests_reusable_call_site_captures_stdout():
    text = (_REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml").read_text()
    assert _CAPTURE_SHAPE.search(text), (
        "tests-reusable.yaml must capture the fetch's stdout into DF_ENV_JSON "
        "via command substitution — echoing it to the log would print every "
        "credential value unmasked"
    )


# ── OIDC path (DATFORG-88) ────────────────────────────────────────────────────


def test_main_uses_oidc_and_endpoint(monkeypatch, capsys):
    """main() exchanges the runner's OIDC token and resolves via the
    datasource-credentials endpoint with the resource pin."""
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://runner.example/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "runner-req-token")

    calls = {}
    monkeypatch.setattr(
        fds, "_github_oidc_token", lambda audience="dataforge": "oidc-jwt"
    )

    def fake_exchange(base_url, oidc_token):
        calls["exchange"] = (base_url, oidc_token)
        return "service-token"

    def fake_endpoint(
        base_url, bearer, datasource, env_tier="", resource_id="", credential_id=""
    ):
        calls["endpoint"] = {
            "bearer": bearer,
            "datasource": datasource,
            "resource_id": resource_id,
            "credential_id": credential_id,
        }
        return {"host": "h", "username": "u", "password": "p", "database": "d"}, "res-1"

    monkeypatch.setattr(fds, "_exchange_for_service_token", fake_exchange)
    monkeypatch.setattr(fds, "resolve_via_endpoint", fake_endpoint)

    assert fds.main(["--datasource", "postgres", "--resource-id", "res-1"]) == 0
    exports = json.loads(capsys.readouterr().out)
    assert exports["E2E_POSTGRES_HOST"] == "h"
    assert calls["exchange"][1] == "oidc-jwt"
    assert calls["endpoint"]["bearer"] == "service-token"
    assert calls["endpoint"]["resource_id"] == "res-1"
    assert calls["endpoint"]["credential_id"] == ""


def test_managed_mode_pin_becomes_credential_id(monkeypatch, capsys):
    monkeypatch.setattr(
        fds, "_github_oidc_token", lambda audience="dataforge": "oidc-jwt"
    )
    monkeypatch.setattr(
        fds, "_exchange_for_service_token", lambda b, o: "service-token"
    )
    seen = {}

    def fake_endpoint(
        base_url, bearer, datasource, env_tier="", resource_id="", credential_id=""
    ):
        seen["resource_id"], seen["credential_id"] = resource_id, credential_id
        return {"client_id": "c", "client_secret": "s"}, "cred-1"

    monkeypatch.setattr(fds, "resolve_via_endpoint", fake_endpoint)
    assert (
        fds.main(
            ["--datasource", "powerbi", "--mode", "managed", "--resource-id", "cred-1"]
        )
        == 0
    )
    assert seen == {"resource_id": "", "credential_id": "cred-1"}
    json.loads(capsys.readouterr().out)


def test_oidc_unavailable_is_actionable(monkeypatch, capsys):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    assert fds.main(["--datasource", "postgres", "--resource-id", "r"]) == 1
    err = capsys.readouterr().err
    assert "id-token" in err and "CI-only" in err


def test_oauth_base_urls_api_host_first_with_app_fallback(monkeypatch):
    """API host first (VPN-reachable), app host as the 404 fallback."""
    monkeypatch.delenv("DATAFORGE_OAUTH_BASE_URL", raising=False)
    assert fds._oauth_base_urls("https://api.dataforge.atlan.dev") == [
        "https://api.dataforge.atlan.dev",
        "https://dataforge.atlan.dev",
    ]
    # Explicit override pins one host; non-api hosts have no fallback twin.
    monkeypatch.setenv("DATAFORGE_OAUTH_BASE_URL", "https://oauth.example/")
    assert fds._oauth_base_urls("https://api.dataforge.atlan.dev") == [
        "https://oauth.example"
    ]
    monkeypatch.delenv("DATAFORGE_OAUTH_BASE_URL", raising=False)
    assert fds._oauth_base_urls("https://dataforge.local") == [
        "https://dataforge.local"
    ]


# ── Token-exchange 404 fallback (the deploy-ordering hedge) ───────────────────


class _FakeResponse:
    """Minimal urlopen context manager returning a fixed JSON payload."""

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def _exchange_stub(monkeypatch, outcomes):
    """Stub urlopen to play *outcomes* (payload dicts or exceptions) in order,
    recording the requested URLs."""
    urls = []

    def fake_urlopen(req, timeout=0):
        urls.append(req.full_url)
        outcome = outcomes[len(urls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)

    monkeypatch.delenv("DATAFORGE_OAUTH_BASE_URL", raising=False)
    monkeypatch.setattr(fds.urllib.request, "urlopen", fake_urlopen)
    return urls


def test_exchange_falls_back_to_app_host_on_api_404(monkeypatch):
    """api 404 (host doesn't route /oauth yet) -> retry the app host, whose
    success returns its token."""
    urls = _exchange_stub(
        monkeypatch,
        [_http_error(404, b"{}"), {"access_token": "svc-from-app"}],
    )
    token = fds._exchange_for_service_token("https://api.dataforge.atlan.dev", "oidc")
    assert token == "svc-from-app"
    assert urls == [
        "https://api.dataforge.atlan.dev/oauth/token",
        "https://dataforge.atlan.dev/oauth/token",
    ]


def test_exchange_all_candidates_404_raises(monkeypatch):
    """Every candidate 404 -> DataforgeSourceError naming the last HTTP code,
    with the workload-binding hint."""
    urls = _exchange_stub(
        monkeypatch, [_http_error(404, b"{}"), _http_error(404, b"{}")]
    )
    with pytest.raises(fds.DataforgeSourceError, match="HTTP 404"):
        fds._exchange_for_service_token("https://api.dataforge.atlan.dev", "oidc")
    assert urls == [
        "https://api.dataforge.atlan.dev/oauth/token",
        "https://dataforge.atlan.dev/oauth/token",
    ]


def test_exchange_non_404_does_not_fall_back(monkeypatch):
    """A non-404 answer IS the exchange's real answer (bad grant, forbidden) —
    surface it immediately instead of retrying the app host."""
    urls = _exchange_stub(monkeypatch, [_http_error(403, b"{}")])
    with pytest.raises(fds.DataforgeSourceError, match="HTTP 403"):
        fds._exchange_for_service_token("https://api.dataforge.atlan.dev", "oidc")
    assert urls == ["https://api.dataforge.atlan.dev/oauth/token"]


def test_exchange_unexpected_response_shape_raises(monkeypatch):
    """Valid JSON without access_token -> body-free DataforgeSourceError, not a
    raw KeyError traceback."""
    _exchange_stub(monkeypatch, [{"token_type": "bearer"}])
    with pytest.raises(fds.DataforgeSourceError, match="token exchange failed"):
        fds._exchange_for_service_token("https://api.dataforge.atlan.dev", "oidc")


# ── Endpoint response-shape validation ─────────────────────────────────────────


def _endpoint_doc_stub(monkeypatch, doc):
    monkeypatch.setattr(fds, "_http_get", lambda url, bearer: doc)


def test_endpoint_rejects_top_level_json_list(monkeypatch):
    """Valid JSON of the wrong shape must not escape as a raw AttributeError —
    it collapses into a fixed, body-free DataforgeSourceError."""
    _endpoint_doc_stub(monkeypatch, ["not", "a", "dict"])
    with pytest.raises(fds.DataforgeSourceError, match="unexpected response shape"):
        fds.resolve_via_endpoint("https://b", "k", "postgres")


def test_endpoint_rejects_list_fields(monkeypatch):
    _endpoint_doc_stub(monkeypatch, {"fields": ["host", "port"]})
    with pytest.raises(fds.DataforgeSourceError, match="unexpected response shape"):
        fds.resolve_via_endpoint("https://b", "k", "postgres")


def test_endpoint_rejects_non_list_mandatory_missing(monkeypatch):
    _endpoint_doc_stub(
        monkeypatch, {"fields": {"host": "h"}, "mandatory_missing": "password"}
    )
    with pytest.raises(fds.DataforgeSourceError, match="unexpected response shape"):
        fds.resolve_via_endpoint("https://b", "k", "postgres")


def test_endpoint_rejects_non_string_mandatory_missing_items(monkeypatch):
    _endpoint_doc_stub(
        monkeypatch, {"fields": {"host": "h"}, "mandatory_missing": [{"f": 1}]}
    )
    with pytest.raises(fds.DataforgeSourceError, match="unexpected response shape"):
        fds.resolve_via_endpoint("https://b", "k", "postgres")


def test_endpoint_accepts_missing_optional_keys(monkeypatch):
    """Absent optional keys are fine — only present-but-wrong-typed keys fail."""
    _endpoint_doc_stub(monkeypatch, {"fields": {"host": "h"}})
    fields, resolved_id = fds.resolve_via_endpoint("https://b", "k", "postgres")
    assert fields == {"host": "h"}
    assert resolved_id == ""
