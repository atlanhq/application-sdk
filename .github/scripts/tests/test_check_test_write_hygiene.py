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
    assert relative in capsys.readouterr().out


@needs_git
@pytest.mark.parametrize("root", ["local", ".hypothesis"])
def test_a_write_under_an_ignored_dir_that_already_existed_fails(
    repo: Path, root: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Collapsed, git would print ``!! local/`` both before and after."""
    (repo / root / "old").mkdir(parents=True)
    (repo / root / "old" / "seed.json").write_text("{}", encoding="utf-8")

    def write(root_dir: Path) -> None:
        (root_dir / root / "old" / "new.json").write_text("{}", encoding="utf-8")

    assert _round_trip(repo, write) == 1
    assert f"{root}/old/new.json" in capsys.readouterr().out


@needs_git
def test_a_modified_tracked_file_fails(repo: Path) -> None:
    def write(root: Path) -> None:
        (root / "tracked.py").write_text("x = 2\n", encoding="utf-8")

    assert _round_trip(repo, write) == 1


@needs_git
def test_pytest_outputs_and_bytecode_pass(
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
            ".venv/lib/site-packages/pkg/__pycache__/mod.cpython-312.pyc",
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


def test_a_long_leak_list_is_truncated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = tmp_path / "snapshot.txt"
    snap.write_text("", encoding="utf-8")
    leaked = "".join(f"!! local/f{i}.json\0" for i in range(hygiene.REPORT_LIMIT + 3))

    assert hygiene.check(snap, status=lambda: leaked) == 1
    out = capsys.readouterr().out
    assert out.count("!! local/") == hygiene.REPORT_LIMIT
    assert "... and 3 more" in out


def test_entries_keep_their_status_code() -> None:
    before = hygiene.parse_entries(" M a.py\0!! local/\0")
    after = hygiene.parse_entries(" M a.py\0!! local/\0?? b.txt\0")
    assert hygiene.new_entries(before, after) == ["?? b.txt"]
