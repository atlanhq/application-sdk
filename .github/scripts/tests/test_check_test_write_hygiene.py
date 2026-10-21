"""The unit leg's write check fails on a write into the checkout (FND-4237).

Red-green against a real git repository, because the property that matters is
how *git* reports a write under an ignored directory — that is exactly what a
plain ``git status --porcelain`` misses.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_test_write_hygiene as hygiene  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed repo ignoring what the SDK's ``.gitignore`` ignores."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".gitignore").write_text(
        "local/\n.hypothesis/\n__pycache__/\n.venv/\ncoverage.xml\nhtmlcov/\n"
        ".coverage\n.coverage.*\n.pytest_cache/\n",
        encoding="utf-8",
    )
    (root / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    monkeypatch.chdir(root)
    return root


def _round_trip(repo: Path, write) -> int:
    snap = repo.parent / "snapshot.txt"
    assert hygiene.main(["snapshot", str(snap)]) == 0
    write(repo)
    return hygiene.main(["check", str(snap)])


@needs_git
def test_clean_run_passes(repo: Path) -> None:
    assert _round_trip(repo, lambda _: None) == 0


@needs_git
@pytest.mark.parametrize(
    "relative",
    [
        "local/tmp/artifacts/apps/default/x.json",
        ".hypothesis/examples/abc",
        "stray.txt",
    ],
)
def test_a_write_into_the_checkout_fails(
    repo: Path, relative: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ignored or not, a new path fails — the ignored ones are the point."""

    def write(root: Path) -> None:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")

    assert _round_trip(repo, write) == 1
    out = capsys.readouterr().out
    assert relative.split("/")[0] in out


@needs_git
def test_a_modified_tracked_file_fails(repo: Path) -> None:
    def write(root: Path) -> None:
        (root / "tracked.py").write_text("x = 2\n", encoding="utf-8")

    assert _round_trip(repo, write) == 1


@needs_git
def test_pytest_outputs_and_writes_inside_existing_ignored_dirs_pass(
    repo: Path,
) -> None:
    def write(root: Path) -> None:
        for relative in (
            "coverage.xml",
            ".coverage",
            ".coverage.host.123",
            "htmlcov/index.html",
            ".pytest_cache/v/cache/nodeids",
            "pkg/__pycache__/mod.cpython-312.pyc",
            ".venv/lib/new.py",
        ):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")

    assert _round_trip(repo, write) == 0


def test_git_failure_fails_the_check(tmp_path: Path) -> None:
    """A git error must not read as an empty status — that would pass."""
    snap = tmp_path / "snapshot.txt"
    snap.write_text("", encoding="utf-8")

    def broken() -> str:
        raise subprocess.CalledProcessError(128, ["git", "status"])

    with pytest.raises(subprocess.CalledProcessError):
        hygiene.check(snap, status=broken)


def test_entries_keep_their_status_code() -> None:
    before = hygiene.parse_entries(" M a.py\0!! local/\0")
    after = hygiene.parse_entries(" M a.py\0!! local/\0?? b.txt\0")
    assert hygiene.new_entries(before, after) == ["?? b.txt"]
