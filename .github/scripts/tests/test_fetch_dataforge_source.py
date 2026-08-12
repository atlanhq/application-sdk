"""Tests for fetch_dataforge_source.py — resolution, normalization, emission.

The script's security contract is split: IT only fetches and normalizes,
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


# ── Normalization ─────────────────────────────────────────────────────────────


def test_normalize_postgres_shape_emits_canonical_and_prefixed():
    maps = fds.load_field_maps()
    out = fds.normalize(_POSTGRES_DATA, "postgres", maps)

    assert out["E2E_SOURCE_HOST"] == "db.example.internal"
    assert out["E2E_SOURCE_PORT"] == "5432"
    assert out["E2E_SOURCE_DATABASE"] == "dataforge"
    assert out["E2E_SOURCE_USERNAME"] == "df_user"
    assert out["E2E_SOURCE_PASSWORD"] == "s3cret"
    # Prefixed aliases — the contract connector secrets-scripts already read.
    assert out["E2E_POSTGRES_HOST"] == "db.example.internal"
    assert out["E2E_POSTGRES_PASSWORD"] == "s3cret"
    # Every raw scalar gets a prefixed alias too.
    assert out["E2E_POSTGRES_JDBC_URL"].startswith("jdbc:postgresql://")


def test_normalize_snowflake_account_maps_to_host():
    maps = fds.load_field_maps()
    raw = {
        "account": "org-acct",
        "username": "svc",
        "password": "pw",
        "warehouse": "WH1",
    }
    out = fds.normalize(raw, "snowflake", maps)
    assert out["E2E_SOURCE_HOST"] == "org-acct"
    assert out["E2E_SOURCE_DATABASE"] == "WH1"
    assert out["E2E_SNOWFLAKE_WAREHOUSE"] == "WH1"


def test_normalize_powerbi_saas_shape():
    maps = fds.load_field_maps()
    raw = {
        "tenant_id": "t-1",
        "client_id": "c-1",
        "client_secret": "cs-1",
        "workspace_id": "w-1",
    }
    out = fds.normalize(raw, "powerbi", maps)
    assert out["E2E_SOURCE_USERNAME"] == "c-1"
    assert out["E2E_SOURCE_PASSWORD"] == "cs-1"
    assert out["E2E_SOURCE_DATABASE"] == "w-1"
    assert out["E2E_SOURCE_HOST"] == ""  # no such concept; empty, not absent
    assert out["E2E_POWERBI_TENANT_ID"] == "t-1"


def test_nested_values_go_to_extra_json_and_raw_holds_scalars():
    maps = fds.load_field_maps()
    out = fds.normalize(_POSTGRES_DATA, "postgres", maps)
    extra = json.loads(out["E2E_SOURCE_EXTRA_JSON"])
    assert extra == {"iam": {"enabled": False}, "metadata": {"request_id": "r-1"}}
    raw = json.loads(out["E2E_SOURCE_RAW_JSON"])
    assert raw["password"] == "s3cret"
    assert "iam" not in raw


def test_field_map_override_wins():
    maps = fds.load_field_maps('{"host": ["weird_endpoint"]}')
    out = fds.normalize({"weird_endpoint": "h1", "host": "wrong"}, "postgres", maps)
    assert out["E2E_SOURCE_HOST"] == "h1"


def test_env_names_are_sanitized_and_output_prefix_respected():
    maps = fds.load_field_maps()
    out = fds.normalize({"https-url": "https://x"}, "metabase", maps, "META_BASE")
    assert out["E2E_META_BASE_HTTPS_URL"] == "https://x"


def test_all_values_are_strings():
    """export_extra_env.parse rejects non-scalars — the hand-off must be safe."""
    maps = fds.load_field_maps()
    out = fds.normalize(_POSTGRES_DATA, "postgres", maps)
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


def test_resolve_resource_happy(monkeypatch):
    monkeypatch.setattr(fds, "_http_get", lambda url, key: _resource_doc())
    assert fds.resolve_resource("https://b", "k", "res-1") == _POSTGRES_DATA


def test_resolve_resource_requires_provisioned(monkeypatch):
    monkeypatch.setattr(fds, "_http_get", lambda url, key: _resource_doc("PAUSED"))
    with pytest.raises(fds.DataforgeSourceError, match="PAUSED"):
        fds.resolve_resource("https://b", "k", "res-1")


def test_resolve_resource_vault_only_is_actionable(monkeypatch):
    monkeypatch.setattr(fds, "_http_get", lambda url, key: _resource_doc(data={}))
    with pytest.raises(fds.DataforgeSourceError, match="category 'ci'"):
        fds.resolve_resource("https://b", "k", "res-1")


# ── Resolution: managed mode ──────────────────────────────────────────────────

_MANAGED_ITEMS = {
    "items": [
        {"ID": "m-dead", "LifecycleStatus": "decommissioned", "TestStatus": "passing"},
        {"ID": "m-untested", "LifecycleStatus": "active", "TestStatus": "untested"},
        {
            "ID": "m-good",
            "LifecycleStatus": "active",
            "TestStatus": "passing",
            "RotatedAt": "2026-08-01T00:00:00Z",
            "LastSeenInVaultAt": "2026-08-02T00:00:00Z",
        },
    ]
}


def test_resolve_managed_filters_decommissioned_and_prefers_passing(monkeypatch):
    monkeypatch.setattr(fds, "_http_get", lambda url, key: _MANAGED_ITEMS)
    revealed = {}

    def fake_post(url, key):
        revealed["url"] = url
        return {"fields": {"host": "h", "password": "p"}}

    monkeypatch.setattr(fds, "_http_post", fake_post)
    fields, cred_id = fds.resolve_managed("https://b", "k", "powerbi")
    assert cred_id == "m-good"
    assert "m-good/reveal" in revealed["url"]
    assert fields == {"host": "h", "password": "p"}


def test_resolve_managed_rotation_picks_newest_passing_entry(monkeypatch):
    """A rotation leaves old + new entries both active+passing until the old
    one is decommissioned. Selection must land on the newest (highest
    RotatedAt), not whichever the API returned first."""
    rotated = {
        "items": [
            {
                "ID": "m-stale",
                "LifecycleStatus": "active",
                "TestStatus": "passing",
                "RotatedAt": "2026-07-01T00:00:00Z",
                "LastSeenInVaultAt": "2026-08-10T00:00:00Z",
            },
            {
                "ID": "m-fresh",
                "LifecycleStatus": "active",
                "TestStatus": "passing",
                "RotatedAt": "2026-08-09T00:00:00Z",
                "LastSeenInVaultAt": "2026-08-10T00:00:00Z",
            },
        ]
    }
    monkeypatch.setattr(fds, "_http_get", lambda url, key: rotated)
    monkeypatch.setattr(
        fds, "_http_post", lambda url, key: {"fields": {"host": "h", "password": "p"}}
    )
    _, cred_id = fds.resolve_managed("https://b", "k", "snowflake")
    assert cred_id == "m-fresh"


def test_resolve_managed_unrotated_entries_fall_back_to_last_seen(monkeypatch):
    """Entries never rotated (RotatedAt null) rank below rotated ones and are
    ordered among themselves by LastSeenInVaultAt (always populated)."""
    items = {
        "items": [
            {
                "ID": "m-old-seen",
                "LifecycleStatus": "active",
                "TestStatus": "passing",
                "RotatedAt": None,
                "LastSeenInVaultAt": "2026-08-01T00:00:00Z",
            },
            {
                "ID": "m-new-seen",
                "LifecycleStatus": "active",
                "TestStatus": "passing",
                "RotatedAt": None,
                "LastSeenInVaultAt": "2026-08-10T00:00:00Z",
            },
        ]
    }
    monkeypatch.setattr(fds, "_http_get", lambda url, key: items)
    monkeypatch.setattr(
        fds, "_http_post", lambda url, key: {"fields": {"host": "h", "password": "p"}}
    )
    _, cred_id = fds.resolve_managed("https://b", "k", "snowflake")
    assert cred_id == "m-new-seen"


def test_recency_compares_instants_across_mixed_utc_offsets():
    """Regression for the lexicographic-string nit: RFC 3339 strings only order
    by instant when every offset matches. 2026-08-09T20:00:00-05:00 is 01:00Z
    Aug 10 — *newer* than 2026-08-09T23:00:00Z even though the string sorts
    earlier. Selection must pick the true newest instant."""
    older_instant_later_string = {
        "ID": "m-zulu",
        "RotatedAt": "2026-08-09T23:00:00Z",  # 23:00Z Aug 9
        "LastSeenInVaultAt": "2026-08-10T00:00:00Z",
    }
    newer_instant_earlier_string = {
        "ID": "m-offset",
        "RotatedAt": "2026-08-09T20:00:00-05:00",  # = 01:00Z Aug 10 (newer)
        "LastSeenInVaultAt": "2026-08-10T00:00:00Z",
    }
    winner = fds._select_credential(
        [older_instant_later_string, newer_instant_earlier_string]
    )
    assert winner == "m-offset"


def test_resolve_managed_no_active_entry_errors(monkeypatch):
    monkeypatch.setattr(fds, "_http_get", lambda url, key: {"items": []})
    with pytest.raises(fds.DataforgeSourceError, match="no active managed credential"):
        fds.resolve_managed("https://b", "k", "tableau")


def test_resolve_managed_empty_reveal_errors(monkeypatch):
    monkeypatch.setattr(fds, "_http_post", lambda url, key: {"fields": {}})
    with pytest.raises(fds.DataforgeSourceError, match="revealed no fields"):
        fds.resolve_managed("https://b", "k", "powerbi", credential_id="m-1")


# ── Entry point ───────────────────────────────────────────────────────────────


def test_main_missing_api_key_fails_before_any_request(monkeypatch, capsys):
    monkeypatch.delenv("DATAFORGE_API_KEY", raising=False)
    assert fds.main(["--datasource", "postgres", "--resource-id", "r"]) == 1
    assert "DATAFORGE_API_KEY" in capsys.readouterr().err


def test_main_resource_mode_requires_resource_id(monkeypatch, capsys):
    monkeypatch.setenv("DATAFORGE_API_KEY", "df_x_y")
    assert fds.main(["--datasource", "postgres"]) == 1
    assert "DATAFORGE_RESOURCE_ID" in capsys.readouterr().err


def test_main_stdout_is_pure_json_and_output_file_gets_resolved_id(
    monkeypatch, tmp_path, capsys
):
    """stdout must be command-substitution-safe: one JSON object, nothing else."""
    monkeypatch.setenv("DATAFORGE_API_KEY", "df_x_y")
    out_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    monkeypatch.setattr(fds, "_http_get", lambda url, key: _resource_doc())

    assert fds.main(["--datasource", "postgres", "--resource-id", "res-1"]) == 0
    captured = capsys.readouterr()
    exports = json.loads(captured.out)  # would raise if stdout carried extras
    assert exports["E2E_POSTGRES_HOST"] == "db.example.internal"
    # Breadcrumbs go to stderr and never include values.
    assert "Resolved dataforge source" in captured.err
    assert "s3cret" not in captured.err
    assert "resolved-id=res-1" in out_file.read_text()


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


def test_field_maps_file_is_well_formed():
    maps = json.loads((_SCRIPTS_DIR / "dataforge_field_maps.json").read_text())
    assert "_default" in maps
    for source, profile in maps.items():
        if source.startswith("_comment"):
            continue
        assert isinstance(profile, dict), source
        for canonical, candidates in profile.items():
            assert canonical in fds._CANONICAL_KEYS, (source, canonical)
            assert isinstance(candidates, list) and candidates, (source, canonical)


# ── OIDC path (DATFORG-88) ────────────────────────────────────────────────────


def test_main_without_api_key_uses_oidc_and_endpoint(monkeypatch, capsys):
    """No DATAFORGE_API_KEY -> exchange the runner's OIDC token and resolve
    via /e2e-credentials with the resource pin — no legacy chain touched."""
    monkeypatch.delenv("DATAFORGE_API_KEY", raising=False)
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
    monkeypatch.delenv("DATAFORGE_API_KEY", raising=False)
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
    monkeypatch.delenv("DATAFORGE_API_KEY", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    assert fds.main(["--datasource", "postgres", "--resource-id", "r"]) == 1
    err = capsys.readouterr().err
    assert "id-token" in err and "DATAFORGE_API_KEY" in err


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
