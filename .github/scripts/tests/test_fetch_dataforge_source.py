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

import fetch_dataforge_source as fds  # noqa: E402

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
        {"ID": "m-good", "LifecycleStatus": "active", "TestStatus": "passing"},
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


def test_composite_action_invokes_scripts_at_real_paths():
    action_dir = _REPO_ROOT / ".github" / "actions" / "dataforge-source"
    action_yaml = (action_dir / "action.yaml").read_text()
    assert _CAPTURE_SHAPE.search(action_yaml), "action must capture fetch stdout"
    for rel in re.findall(r"\$\{\{ github\.action_path \}\}/(\S+?\.py)", action_yaml):
        resolved = (action_dir / rel).resolve()
        assert resolved.is_file(), f"action.yaml references missing script {rel}"


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
