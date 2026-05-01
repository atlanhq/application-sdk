"""Tests for .github/scripts/parse_atlan_yaml.py."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from parse_atlan_yaml import (
    AtlanYamlError,
    _read_sdk_version_from_uv_lock,
    _validate_entrypoints,
    parse,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, filename: str, content: str) -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content))
    return p


MINIMAL_YAML = """\
name: my-connector
app_id: abc123
build_tag: v1
"""

FULL_YAML = """\
name: my-connector
app_id: abc123
build_tag: v1
dockerfile: ./Dockerfile.custom
self_deployed_runtime: true
deploy:
  replicas: 2
  resources:
    limits:
      memory: 512Mi
entrypoints:
  - name: extract
    display_name: Extract
    type: connector
  - name: load
    display_name: Load
    type: connector
"""

MINIMAL_UV_LOCK = """\
[[package]]
name = "some-lib"
version = "1.0.0"

[[package]]
name = "atlan-application-sdk"
version = "3.0.1"
source = { registry = "https://pypi.org/simple" }
"""


# ---------------------------------------------------------------------------
# _validate_entrypoints
# ---------------------------------------------------------------------------


class TestValidateEntrypoints:
    def test_none_returns_empty(self) -> None:
        assert _validate_entrypoints(None) == ""

    def test_empty_list_returns_empty(self) -> None:
        assert _validate_entrypoints([]) == ""

    def test_valid_single_entrypoint(self) -> None:
        pkgs = [{"name": "extract", "display_name": "Extract", "type": "connector"}]
        result = _validate_entrypoints(pkgs)
        parsed = json.loads(result)
        assert parsed[0]["name"] == "extract"
        assert parsed[0]["generated_dir"] == "extract"

    def test_invalid_kebab_case_uppercase(self) -> None:
        pkgs = [{"name": "MyConnector", "display_name": "X", "type": "connector"}]
        with pytest.raises(AtlanYamlError, match="kebab-case"):
            _validate_entrypoints(pkgs)

    def test_invalid_kebab_case_with_underscore(self) -> None:
        pkgs = [{"name": "my_connector", "display_name": "X", "type": "connector"}]
        with pytest.raises(AtlanYamlError, match="kebab-case"):
            _validate_entrypoints(pkgs)

    def test_invalid_kebab_case_starts_with_digit(self) -> None:
        pkgs = [{"name": "1connector", "display_name": "X", "type": "connector"}]
        with pytest.raises(AtlanYamlError, match="kebab-case"):
            _validate_entrypoints(pkgs)

    def test_duplicate_entrypoint_names(self) -> None:
        pkgs = [
            {"name": "extract", "display_name": "A", "type": "connector"},
            {"name": "extract", "display_name": "B", "type": "connector"},
        ]
        with pytest.raises(AtlanYamlError, match="duplicated"):
            _validate_entrypoints(pkgs)

    def test_unsupported_type(self) -> None:
        pkgs = [{"name": "my-app", "display_name": "X", "type": "unknown-type"}]
        with pytest.raises(AtlanYamlError, match="type must be one of"):
            _validate_entrypoints(pkgs)

    def test_all_allowed_types(self) -> None:
        for t in ("connector", "miner", "orchestrator", "utility", "custom"):
            pkgs = [{"name": "my-app", "display_name": "X", "type": t}]
            assert json.loads(_validate_entrypoints(pkgs))[0]["type"] == t

    def test_missing_required_key(self) -> None:
        pkgs = [{"name": "my-app", "type": "connector"}]  # missing display_name
        with pytest.raises(AtlanYamlError, match="missing required keys"):
            _validate_entrypoints(pkgs)

    def test_generated_dir_mismatch_raises(self) -> None:
        pkgs = [
            {
                "name": "extract",
                "display_name": "X",
                "type": "connector",
                "generated_dir": "different",
            }
        ]
        with pytest.raises(AtlanYamlError, match="generated_dir must equal name"):
            _validate_entrypoints(pkgs)

    def test_generated_dir_matching_is_ok(self) -> None:
        pkgs = [
            {
                "name": "extract",
                "display_name": "X",
                "type": "connector",
                "generated_dir": "extract",
            }
        ]
        result = json.loads(_validate_entrypoints(pkgs))
        assert result[0]["generated_dir"] == "extract"

    def test_output_is_single_line_json(self) -> None:
        pkgs = [{"name": "my-app", "display_name": "My App", "type": "connector"}]
        result = _validate_entrypoints(pkgs)
        assert "\n" not in result

    def test_empty_display_name_raises(self) -> None:
        pkgs = [{"name": "my-app", "display_name": "  ", "type": "connector"}]
        with pytest.raises(AtlanYamlError, match="non-empty string"):
            _validate_entrypoints(pkgs)

    def test_not_a_list_raises(self) -> None:
        with pytest.raises(AtlanYamlError, match="must be a list"):
            _validate_entrypoints({"name": "bad"})


# ---------------------------------------------------------------------------
# _read_sdk_version_from_uv_lock
# ---------------------------------------------------------------------------


class TestReadSdkVersionFromUvLock:
    def test_reads_version(self, tmp_path: Path) -> None:
        lock = _write(tmp_path, "uv.lock", MINIMAL_UV_LOCK)
        assert _read_sdk_version_from_uv_lock(str(lock)) == "3.0.1"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _read_sdk_version_from_uv_lock(str(tmp_path / "missing.lock")) == ""

    def test_sdk_not_in_lock_returns_empty(self, tmp_path: Path) -> None:
        lock = _write(
            tmp_path, "uv.lock", '[[package]]\nname = "other"\nversion = "1.0"\n'
        )
        assert _read_sdk_version_from_uv_lock(str(lock)) == ""

    def test_multiline_lock_correct_version(self, tmp_path: Path) -> None:
        content = (
            '[[package]]\nname = "atlan-application-sdk"\nversion = "3.1.0"\n'
            '[[package]]\nname = "other"\nversion = "9.9.9"\n'
        )
        lock = _write(tmp_path, "uv.lock", content)
        assert _read_sdk_version_from_uv_lock(str(lock)) == "3.1.0"


# ---------------------------------------------------------------------------
# parse() end-to-end
# ---------------------------------------------------------------------------


class TestParse:
    def test_minimal_yaml(self, tmp_path: Path) -> None:
        yaml_file = _write(tmp_path, "atlan.yaml", MINIMAL_YAML)
        result = parse(str(yaml_file), uv_lock_path=str(tmp_path / "missing.lock"))
        assert result["app_name"] == "my-connector"
        assert result["app_id"] == "abc123"
        assert result["build_tag"] == "v1"
        assert result["dockerfile"] == "./Dockerfile"
        assert result["enable_sdr"] == "false"
        assert result["entrypoints"] == ""
        assert result["sdk_version"] == ""

    def test_full_yaml(self, tmp_path: Path) -> None:
        yaml_file = _write(tmp_path, "atlan.yaml", FULL_YAML)
        lock_file = _write(tmp_path, "uv.lock", MINIMAL_UV_LOCK)
        result = parse(str(yaml_file), str(lock_file))
        assert result["enable_sdr"] == "true"
        assert result["dockerfile"] == "./Dockerfile.custom"
        assert result["sdk_version"] == "3.0.1"
        eps = json.loads(result["entrypoints"])
        assert len(eps) == 2
        assert eps[0]["name"] == "extract"
        assert eps[1]["name"] == "load"

    def test_name_is_lowercased(self, tmp_path: Path) -> None:
        yaml_file = _write(
            tmp_path, "atlan.yaml", "name: MyConnector\napp_id: x\nbuild_tag: v1\n"
        )
        result = parse(str(yaml_file), str(tmp_path / "missing.lock"))
        assert result["app_name"] == "myconnector"

    def test_missing_name_raises(self, tmp_path: Path) -> None:
        yaml_file = _write(tmp_path, "atlan.yaml", "app_id: x\nbuild_tag: v1\n")
        with pytest.raises(AtlanYamlError, match="missing required"):
            parse(str(yaml_file))

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AtlanYamlError, match="not found"):
            parse(str(tmp_path / "missing.yaml"))

    def test_unsupported_build_tag_raises(self, tmp_path: Path) -> None:
        yaml_file = _write(
            tmp_path, "atlan.yaml", "name: x\napp_id: y\nbuild_tag: v2\n"
        )
        with pytest.raises(AtlanYamlError, match="Unsupported build_tag"):
            parse(str(yaml_file))

    def test_deploy_block_in_output(self, tmp_path: Path) -> None:
        content = "name: x\napp_id: y\nbuild_tag: v1\ndeploy:\n  replicas: 3\n"
        yaml_file = _write(tmp_path, "atlan.yaml", content)
        result = parse(str(yaml_file), str(tmp_path / "missing.lock"))
        assert "replicas" in result["deploy_config"]

    def test_no_deploy_block_gives_empty(self, tmp_path: Path) -> None:
        yaml_file = _write(tmp_path, "atlan.yaml", MINIMAL_YAML)
        result = parse(str(yaml_file), str(tmp_path / "missing.lock"))
        assert result["deploy_config"] == ""
