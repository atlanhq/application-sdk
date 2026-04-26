"""Tests for .github/scripts/validate_configmap_contract.py."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from validate_configmap_contract import (
    ConfigMapContractError,
    _read_top_level_name_without_yaml,
    generated_configmap_stems,
    validate_configmap_contract,
)


def _write(path: Path, content: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def test_saphana_contract_matches_generated_stem(tmp_path: Path) -> None:
    atlan_yaml = _write(tmp_path / "atlan.yaml", "name: saphana\n")
    generated_dir = tmp_path / "app" / "generated"
    _write(generated_dir / "saphana.json")

    expected_stem, stems = validate_configmap_contract(atlan_yaml, generated_dir)

    assert expected_stem == "saphana"
    assert stems == ["saphana"]


def test_saphana_contract_rejects_sap_hana_generated_stem(tmp_path: Path) -> None:
    atlan_yaml = _write(tmp_path / "atlan.yaml", "name: saphana\n")
    generated_dir = tmp_path / "app" / "generated"
    _write(generated_dir / "sap-hana.json")

    with pytest.raises(ConfigMapContractError) as exc_info:
        validate_configmap_contract(atlan_yaml, generated_dir)

    message = exc_info.value.message
    assert "Expected generated configmap stem: saphana" in message
    assert "Found generated configmap stems: sap-hana" in message


def test_nested_generated_files_are_checked_and_manifests_ignored(
    tmp_path: Path,
) -> None:
    atlan_yaml = _write(tmp_path / "atlan.yaml", "name: my-app\n")
    generated_dir = tmp_path / "app" / "generated"
    _write(generated_dir / "manifest.json")
    _write(generated_dir / "entrypoint" / "manifest.json")
    _write(generated_dir / "entrypoint" / "my-app.json")

    expected_stem, stems = validate_configmap_contract(atlan_yaml, generated_dir)

    assert expected_stem == "my-app"
    assert stems == ["my-app"]


def test_generated_configmap_stems_deduplicates_stems(tmp_path: Path) -> None:
    generated_dir = tmp_path / "app" / "generated"
    _write(generated_dir / "config-a.json")
    _write(generated_dir / "nested" / "config-a.json")
    _write(generated_dir / "nested" / "config-b.json")

    assert generated_configmap_stems(generated_dir) == ["config-a", "config-b"]


def test_missing_generated_dir_is_rejected(tmp_path: Path) -> None:
    atlan_yaml = _write(tmp_path / "atlan.yaml", "name: my-app\n")

    with pytest.raises(ConfigMapContractError, match="not found"):
        validate_configmap_contract(atlan_yaml, tmp_path / "app" / "generated")


def test_missing_atlan_yaml_name_is_rejected(tmp_path: Path) -> None:
    atlan_yaml = _write(tmp_path / "atlan.yaml", "app_id: abc\n")
    generated_dir = tmp_path / "app" / "generated"
    _write(generated_dir / "my-app.json")

    with pytest.raises(ConfigMapContractError, match="missing required"):
        validate_configmap_contract(atlan_yaml, generated_dir)


def test_fallback_top_level_name_parser_handles_comments_and_quotes(
    tmp_path: Path,
) -> None:
    atlan_yaml = _write(
        tmp_path / "atlan.yaml",
        """
        # leading comment
        name: "sap#hana" # inline comment
        deploy:
          name: nested-name
        """,
    )

    assert _read_top_level_name_without_yaml(atlan_yaml) == "sap#hana"
