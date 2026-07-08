"""Tests for .github/actions/sdr-e2e/build_compose_chain.py.

See test_select_dapr_components.py for why the module under test lives
under .github/actions/sdr-e2e/ rather than .github/scripts/, and why the
test itself still lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "actions" / "sdr-e2e"))

from build_compose_chain import build_compose_files, main  # noqa: E402


def test_base_and_sdk_only_when_no_app_overlay_and_no_two_store(
    tmp_path: Path,
) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"

    files = build_compose_files(base, sdk, "", "", two_store=False)

    assert files == [str(base), str(sdk)]


def test_explicit_app_overlay_wins_when_file_exists(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"
    overlay = tmp_path / "app-overlay.yaml"
    overlay.write_text("services: {}")

    files = build_compose_files(base, sdk, str(overlay), "", two_store=False)

    assert files == [str(base), str(sdk), str(overlay)]


def test_falls_back_to_sdr_config_dir_convention(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"
    sdr_dir = tmp_path / ".github" / "e2e"
    sdr_dir.mkdir(parents=True)
    (sdr_dir / "docker-compose.ci.yml").write_text("services: {}")

    files = build_compose_files(base, sdk, "", str(sdr_dir), two_store=False)

    assert files[-1] == str(sdr_dir / "docker-compose.ci.yml")


def test_missing_overlay_file_is_skipped(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"

    files = build_compose_files(
        base, sdk, str(tmp_path / "does-not-exist.yaml"), "", two_store=False
    )

    assert files == [str(base), str(sdk)]


def test_two_store_appends_overlay_last_after_app_overlay(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"
    overlay = tmp_path / "app-overlay.yaml"
    overlay.write_text("services: {}")
    two_store_compose = tmp_path / "docker-compose.two-store.yml"

    files = build_compose_files(
        base,
        sdk,
        str(overlay),
        "",
        two_store=True,
        two_store_compose=two_store_compose,
    )

    assert files == [str(base), str(sdk), str(overlay), str(two_store_compose)]


def test_two_store_without_app_overlay(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"
    two_store_compose = tmp_path / "docker-compose.two-store.yml"

    files = build_compose_files(
        base, sdk, "", "", two_store=True, two_store_compose=two_store_compose
    )

    assert files == [str(base), str(sdk), str(two_store_compose)]


def test_two_store_requires_compose_path() -> None:
    with pytest.raises(ValueError):
        build_compose_files(Path("a"), Path("b"), "", "", two_store=True)


def test_two_store_compose_appended_even_if_it_does_not_exist_on_disk(
    tmp_path: Path,
) -> None:
    # docker-compose.two-store.yml is SDK-owned and always shipped alongside
    # this script; unlike the app overlay it isn't existence-checked.
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"
    two_store_compose = tmp_path / "does-not-exist.yml"

    files = build_compose_files(
        base, sdk, "", "", two_store=True, two_store_compose=two_store_compose
    )

    assert str(two_store_compose) in files


def test_main_emits_files_output_line(tmp_path: Path, capsys) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"
    two_store_compose = tmp_path / "two-store.yml"

    rc = main(
        [
            "--base-compose",
            str(base),
            "--sdk-compose",
            str(sdk),
            "--two-store",
            "true",
            "--two-store-compose",
            str(two_store_compose),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == f"files=-f {base} -f {sdk} -f {two_store_compose}"


def test_main_single_store_default(tmp_path: Path, capsys) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"

    rc = main(["--base-compose", str(base), "--sdk-compose", str(sdk)])

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == f"files=-f {base} -f {sdk}"


def test_main_two_store_without_compose_path_errors(tmp_path: Path, capsys) -> None:
    base = tmp_path / "base.yaml"
    sdk = tmp_path / "sdk-ci.yaml"

    rc = main(
        [
            "--base-compose",
            str(base),
            "--sdk-compose",
            str(sdk),
            "--two-store",
            "true",
        ]
    )

    assert rc == 1
    assert "::error::" in capsys.readouterr().err
