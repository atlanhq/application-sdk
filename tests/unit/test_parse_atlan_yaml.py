"""Unit tests for .github/scripts/parse_atlan_yaml.py.

The parser is invoked from the Build & Publish reusable workflow to read
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
    assert out["workflows"] == ""  # absent → consumers fall back to argo_package_names
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
        "[[package]]\n" 'name = "atlan-application-sdk"\n' 'version = "0.1.10"\n'
    )
    out = parse_atlan_yaml.parse()
    assert out["sdk_version"] == "0.1.10"


# ── workflows happy path ────────────────────────────────────────────


def _pkg(
    name="teradata-crawler", display="Teradata Crawler", typ="connector", gen="crawler"
):
    return {
        "name": name,
        "display_name": display,
        "description": f"{name} description",
        "icon_url": "https://assets.atlan.com/assets/Teradata.svg",
        "type": typ,
        "generated_dir": gen,
    }


def test_workflows_round_trips_as_compact_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pkgs = [_pkg(), _pkg("teradata-miner", "Teradata Miner", "miner", "miner")]
    _write(tmp_path, {"name": "teradata", "app_id": "1", "workflows": pkgs})
    out = parse_atlan_yaml.parse()
    # Single line — required because the workflow appends to $GITHUB_OUTPUT
    # via simple key=value (multiline values would need delimiter syntax).
    assert "\n" not in out["workflows"]
    assert json.loads(out["workflows"]) == pkgs


# ── workflows validation rules (must mirror GM Pydantic) ────────────


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
            "workflows": [_pkg(name=bad_name)],
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
    _write(tmp_path, {"name": "x", "app_id": "1", "workflows": [pkg]})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_duplicate_names_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(
        tmp_path,
        {
            "name": "x",
            "app_id": "1",
            "workflows": [_pkg(), _pkg()],  # two identical names
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
            "workflows": [{"name": "x", "type": "connector"}],
        },
    )
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_workflows_must_be_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path, {"name": "x", "app_id": "1", "workflows": {"name": "x"}})
    with pytest.raises(AtlanYamlError):
        parse_atlan_yaml.parse()


def test_empty_display_name_or_generated_dir_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pkg = _pkg(display="")
    _write(tmp_path, {"name": "x", "app_id": "1", "workflows": [pkg]})
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
