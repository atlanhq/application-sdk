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


def test_disjoint_wheels_pass(tmp_path: Path) -> None:
    api = _wheel(tmp_path / "api.whl", {"application_sdk/__init__.py": b"x"})
    sdk = _wheel(tmp_path / "sdk.whl", {"application_sdk/main.py": b"z"})
    tracked = {"application_sdk/__init__.py", "application_sdk/main.py"}
    assert probe.partition(api, sdk, ["application_sdk/__init__.py"], tracked) == []


def test_overlap_misplacement_and_gaps_fail(tmp_path: Path) -> None:
    api = _wheel(
        tmp_path / "api.whl",
        {"application_sdk/a.py": b"1", "application_sdk/stray.py": b"2"},
    )
    sdk = _wheel(
        tmp_path / "sdk.whl",
        {"application_sdk/a.py": b"1", "application_sdk/main.py": b"3"},
    )
    listed = ["application_sdk/a.py", "application_sdk/b.py"]
    tracked = {
        "application_sdk/a.py",
        "application_sdk/b.py",
        "application_sdk/main.py",
        "application_sdk/lost.py",
    }
    assert probe.partition(api, sdk, listed, tracked) == [
        "application_sdk/a.py: ships in both wheels",
        "application_sdk/b.py: listed but not in the api wheel",
        "application_sdk/stray.py: in the api wheel but not listed",
        "application_sdk/b.py: in neither wheel",
        "application_sdk/lost.py: in neither wheel",
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
