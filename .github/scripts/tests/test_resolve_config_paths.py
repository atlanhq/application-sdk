"""Tests for .github/actions/sdr-e2e/resolve_config_paths.py.

See test_select_dapr_components.py for why the module under test lives
under .github/actions/sdr-e2e/ rather than .github/scripts/, and why the
test itself still lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

from resolve_config_paths import main, resolve_paths  # noqa: E402


def _mkdir(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# resolve_paths — config dir resolution
# ---------------------------------------------------------------------------


def test_explicit_config_dir_wins_when_it_exists(tmp_path: Path) -> None:
    custom = _mkdir(tmp_path, "custom/dir")
    (custom / "app.yaml").write_text("x")

    config_dir, app_yaml = resolve_paths("custom/dir", root=tmp_path)

    assert config_dir == "custom/dir"
    assert app_yaml == "custom/dir/app.yaml"


def test_explicit_config_dir_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="config-dir 'nope' not found"):
        resolve_paths("nope", root=tmp_path)


def test_prefers_sdr_e2e_over_e2e(tmp_path: Path) -> None:
    sdr = _mkdir(tmp_path, ".github/sdr-e2e")
    _mkdir(tmp_path, ".github/e2e")
    (sdr / "app.yaml").write_text("x")

    config_dir, app_yaml = resolve_paths("", root=tmp_path)

    assert config_dir == ".github/sdr-e2e"
    assert app_yaml == ".github/sdr-e2e/app.yaml"


def test_falls_back_to_legacy_e2e_dir(tmp_path: Path) -> None:
    e2e = _mkdir(tmp_path, ".github/e2e")
    (e2e / "app.yaml").write_text("x")

    config_dir, app_yaml = resolve_paths("", root=tmp_path)

    assert config_dir == ".github/e2e"
    assert app_yaml == ".github/e2e/app.yaml"


def test_no_config_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No SDR config dir found"):
        resolve_paths("", root=tmp_path)


# ---------------------------------------------------------------------------
# resolve_paths — app.yaml resolution
# ---------------------------------------------------------------------------


def test_app_yaml_in_config_dir_wins_over_root(tmp_path: Path) -> None:
    sdr = _mkdir(tmp_path, ".github/sdr-e2e")
    (sdr / "app.yaml").write_text("x")
    (tmp_path / "app.yaml").write_text("root")

    _, app_yaml = resolve_paths("", root=tmp_path)

    assert app_yaml == ".github/sdr-e2e/app.yaml"


def test_app_yaml_falls_back_to_repo_root(tmp_path: Path) -> None:
    _mkdir(tmp_path, ".github/sdr-e2e")  # dir present, no app.yaml inside
    (tmp_path / "app.yaml").write_text("root")

    config_dir, app_yaml = resolve_paths("", root=tmp_path)

    assert config_dir == ".github/sdr-e2e"
    assert app_yaml == "app.yaml"


def test_no_app_yaml_anywhere_raises(tmp_path: Path) -> None:
    _mkdir(tmp_path, ".github/sdr-e2e")  # dir present, no app.yaml anywhere

    with pytest.raises(ValueError, match="No app.yaml found"):
        resolve_paths("", root=tmp_path)


# ---------------------------------------------------------------------------
# main — GITHUB_ENV lines on stdout, ::error:: + rc on failure
# ---------------------------------------------------------------------------


def test_main_emits_github_env_lines(tmp_path: Path, capsys) -> None:
    sdr = _mkdir(tmp_path, ".github/sdr-e2e")
    (sdr / "app.yaml").write_text("x")

    rc = main(["--config-dir", "", "--root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert "SDR_CONFIG_DIR=.github/sdr-e2e" in out
    assert "APP_YAML_PATH=.github/sdr-e2e/app.yaml" in out


def test_main_error_writes_error_annotation_and_returns_1(
    tmp_path: Path, capsys
) -> None:
    rc = main(["--config-dir", "", "--root", str(tmp_path)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "::error::" in captured.err
    # No env lines leak to stdout on the failure path.
    assert captured.out == ""
