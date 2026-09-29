"""Tests for probe_api_wheels.py's pure helpers (the install legs run in CI)."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import probe_api_wheels as probe  # noqa: E402


def _wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def test_identical_files_pass(tmp_path: Path) -> None:
    files = {
        "application_sdk/__init__.py": b"x",
        "application_sdk/errors/base.py": b"y",
    }
    api = _wheel(tmp_path / "api.whl", files)
    sdk = _wheel(tmp_path / "sdk.whl", {**files, "application_sdk/main.py": b"z"})
    assert probe.same_bytes(api, sdk) == []


def test_a_differing_or_missing_file_fails(tmp_path: Path) -> None:
    api = _wheel(
        tmp_path / "api.whl",
        {"application_sdk/a.py": b"new", "application_sdk/b.py": b"only here"},
    )
    sdk = _wheel(tmp_path / "sdk.whl", {"application_sdk/a.py": b"old"})
    assert probe.same_bytes(api, sdk) == [
        "application_sdk/a.py: differs in the SDK wheel",
        "application_sdk/b.py: missing from the SDK wheel",
    ]


def test_an_explicit_release_is_used_as_is(tmp_path: Path) -> None:
    assert probe.resolve_release("3.39.1", tmp_path) == "3.39.1"


def test_latest_tag_reads_the_newest_reachable_release(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "a",
    )
    git("tag", "v3.39.0")
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "b",
    )
    git("tag", "v3.39.1")
    git("tag", "conformance-v0.40.1")
    assert probe.resolve_release("latest-tag", tmp_path) == "3.39.1"
    # The built wheels carry the released version right after a release: probe
    # the upgrade from the release before it, never from itself.
    assert probe.resolve_release("latest-tag", tmp_path, below="3.39.1") == "3.39.0"
    assert probe.resolve_release("latest-tag", tmp_path, below="3.40.0") == "3.39.1"
