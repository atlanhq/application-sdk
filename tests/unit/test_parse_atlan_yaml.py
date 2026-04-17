"""Unit tests for .github/scripts/parse_atlan_yaml.py.

The parser is invoked from the Build & Publish reusable entrypoint to read
``atlan.yaml`` (and optionally ``uv.lock``) and emit values that downstream
jobs forward to GHCR + the marketplace publish endpoint. Validation here
fails fast at CI time with annotated errors — these tests pin the rules
that any future tweak must keep honouring.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

# Load the script as a module without forcing a package layout under .github.
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "parse_atlan_yaml.py"
)
_spec = importlib.util.spec_from_file_location("parse_atlan_yaml", _SCRIPT_PATH)
parse_atlan_yaml = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["parse_atlan_yaml"] = parse_atlan_yaml
assert _spec and _spec.loader
_spec.loader.exec_module(parse_atlan_yaml)  # type: ignore[union-attr]

AtlanYamlError = parse_atlan_yaml.AtlanYamlError


def _write(tmp_path: Path, payload: dict, name: str = "atlan.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(payload))
    return p


# ── happy path ──────────────────────────────────────────────────────────────


def test_parse_minimal_atlan_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path, {"name": "Postgres", "app_id": "abc", "self_deployed_runtime": False}
    )
    out = parse_atlan_yaml.parse()
    assert out["app_name"] == "postgres"  # lowercased
    assert out["app_id"] == "abc"
    assert out["enable_sdr"] == "false"
    assert out["dockerfile"] == "./Dockerfile"
    assert out["build_tag"] == "v1"
    assert out["sdk_version"] == ""  # no uv.lock
    assert (
        out["entrypoints"] == ""
    )  # absent → consumers fall back to argo_package_names
    assert out["deploy_config"] == ""


def test_parse_emits_deploy_config_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {
            "name": "x",
            "app_id": "1",
            "self_deployed_runtime": True,
            "deploy": {"replicaCount": 2, "containerPort": 8000},
        },
    )
    out = parse_atlan_yaml.parse()
    assert out["enable_sdr"] == "true"
    assert "replicaCount: 2" in out["deploy_config"]


def test_parse_reads_sdk_version_from_uv_lock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"name": "x", "app_id": "1"})
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "atlan-application-sdk"\nversion = "0.1.10"\n'
    )
    out = parse_atlan_yaml.parse()
    assert out["sdk_version"] == "0.1.10"


# ── entrypoints happy path ────────────────────────────────────────────


def _pkg(name="teradata-crawler", display="Teradata Crawler", typ="connector"):
    return {
        "name": name,
        "display_name": display,
        "description": f"{name} description",
        "icon_url": "https://assets.atlan.com/assets/Teradata.svg",
        "type": typ,
        # generated_dir omitted — defaults to name (Option B convention)
    }


def test_entrypoints_round_trips_as_compact_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pkgs = [_pkg(), _pkg("teradata-miner", "Teradata Miner", "miner")]
    _write(tmp_path, {"name": "teradata", "app_id": "1", "entrypoints": pkgs})
    out = parse_atlan_yaml.parse()
    # Single line — required because the entrypoint appends to $GITHUB_OUTPUT
    # via simple key=value (multiline values would need delimiter syntax).
    assert "\n" not in out["entrypoints"]
    # generated_dir is auto-filled to equal name; check it appears in output.
    parsed = json.loads(out["entrypoints"])
    assert parsed[0]["generated_dir"] == "teradata-crawler"
    assert parsed[1]["generated_dir"] == "teradata-miner"


def test_generated_dir_defaults_to_name(tmp_path, monkeypatch):
    """Omitting generated_dir auto-sets it to the entrypoint name."""
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {"name": "x", "app_id": "1", "entrypoints": [_pkg("my-crawler")]},
    )
    out = parse_atlan_yaml.parse()
    parsed = json.loads(out["entrypoints"])
    assert parsed[0]["generated_dir"] == "my-crawler"


def test_generated_dir_equal_to_name_accepted(tmp_path, monkeypatch):
    """Explicitly setting generated_dir == name is allowed."""
    monkeypatch.chdir(tmp_path)
    pkg = _pkg("my-crawler")
    pkg["generated_dir"] = "my-crawler"
    _write(tmp_path, {"name": "x", "app_id": "1", "entrypoints": [pkg]})
    out = parse_atlan_yaml.parse()
    parsed = json.loads(out["entrypoints"])
    assert parsed[0]["generated_dir"] == "my-crawler"


def test_generated_dir_mismatch_rejected(tmp_path, monkeypatch):
    """generated_dir that differs from name raises AtlanYamlError."""
    monkeypatch.chdir(tmp_path)
    pkg = _pkg("my-crawler")
    pkg["generated_dir"] = "something-else"
    _write(tmp_path, {"name": "x", "app_id": "1", "entrypoints": [pkg]})
    with pytest.raises(AtlanYamlError, match="generated_dir must equal name"):
        parse_atlan_yaml.parse()


# ── entrypoints validation rules (must mirror GM Pydantic) ────────────


@pytest.mark.parametrize(
    "bad_name",
    ["Teradata-Miner", "teradata_miner", "teradata miner", "-tera", "tera-", "UPPER"],
)
def test_kebab_case_name_rejected(tmp_path, monkeypatch, bad_name):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {
            "name": "x",
            "app_id": "1",
            "entrypoints": [_pkg(name=bad_name)],
        },
    )
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


@pytest.mark.parametrize(
    "bad_type",
    ["Connector", "database", "", None, "miner-v2"],
)
def test_type_must_be_in_closed_set(tmp_path, monkeypatch, bad_type):
    monkeypatch.chdir(tmp_path)
    pkg = _pkg()
    pkg["type"] = bad_type
    _write(tmp_path, {"name": "x", "app_id": "1", "entrypoints": [pkg]})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_duplicate_names_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {
            "name": "x",
            "app_id": "1",
            "entrypoints": [_pkg(), _pkg()],  # two identical names
        },
    )
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_missing_required_keys_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {
            "name": "x",
            "app_id": "1",
            "entrypoints": [{"name": "x", "type": "connector"}],
        },
    )
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_entrypoints_must_be_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"name": "x", "app_id": "1", "entrypoints": {"name": "x"}})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_empty_display_name_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pkg = _pkg(display="")
    _write(tmp_path, {"name": "x", "app_id": "1", "entrypoints": [pkg]})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


# ── top-level required fields ───────────────────────────────────────────────


def test_missing_atlan_yaml_name_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"app_id": "1"})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_unsupported_build_tag_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"name": "x", "app_id": "1", "build_tag": "v2"})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_missing_atlan_yaml_file_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir, no atlan.yaml
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_invalid_yaml_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlan.yaml").write_text("name: x\n  bad: : indent\n  - unclosed")
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()
