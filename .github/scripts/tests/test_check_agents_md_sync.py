"""Every AGENTS.md is the generated pointer at the CLAUDE.md beside it (FND-4273)."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import check_agents_md_sync as sync
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A git repo whose tracked files are *files*."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", *files], cwd=tmp_path, check=True)
    return tmp_path


def _run(root: Path, *, fix: bool = False) -> list[str]:
    return sync.check(root, sync.tracked_guides(root), fix=fix)


def test_the_repository_passes() -> None:
    """The real tree: the check CI runs, as a test that fails on the PR."""
    assert _run(REPO_ROOT) == []


def test_the_repository_has_the_pointers_it_is_meant_to() -> None:
    """Guards the guard: a glob that matched nothing would pass vacuously."""
    paths = sync.tracked_guides(REPO_ROOT)
    assert PurePosixPath("AGENTS.md") in paths
    assert PurePosixPath("contract-toolkit/AGENTS.md") in paths


def test_pointer_names_its_guide_and_this_script() -> None:
    text = sync.render()
    assert "[`CLAUDE.md`](CLAUDE.md)" in text
    assert sync.SCRIPT in text
    assert (REPO_ROOT / sync.SCRIPT).is_file()


def test_rendered_pointers_pass(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "CLAUDE.md": "# guide\n",
            "AGENTS.md": sync.render(),
            "sub/CLAUDE.md": "# sub guide\n",
            "sub/AGENTS.md": sync.render(),
        },
    )
    assert _run(root) == []


def test_content_added_to_agents_md_fails(tmp_path: Path) -> None:
    """The drift this exists for: a rule written into AGENTS.md only."""
    root = _repo(
        tmp_path,
        {"CLAUDE.md": "# guide\n", "AGENTS.md": sync.render() + "- a new rule\n"},
    )
    assert _run(root) == [
        "AGENTS.md: differs from the generated pointer; put content in "
        "CLAUDE.md, not here"
    ]


def test_nested_drift_is_found(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "CLAUDE.md": "# guide\n",
            "AGENTS.md": sync.render(),
            "pkg/CLAUDE.md": "# pkg\n",
            "pkg/AGENTS.md": "# a hand-written copy\n",
        },
    )
    assert [p.split(":")[0] for p in _run(root)] == ["pkg/AGENTS.md"]


def test_claude_md_without_agents_md_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"pkg/CLAUDE.md": "# pkg\n"})
    assert _run(root) == ["pkg/AGENTS.md: missing; every CLAUDE.md needs one"]


def test_agents_md_without_claude_md_fails_and_fix_leaves_it(tmp_path: Path) -> None:
    root = _repo(tmp_path, {"pkg/AGENTS.md": "# the only guide\n"})
    problems = _run(root, fix=True)
    assert len(problems) == 1
    assert problems[0].startswith("pkg/AGENTS.md: no CLAUDE.md beside it")
    assert (root / "pkg/AGENTS.md").read_text() == "# the only guide\n"


def test_fix_rewrites_and_creates_pointers(tmp_path: Path) -> None:
    root = _repo(
        tmp_path,
        {
            "CLAUDE.md": "# guide\n",
            "AGENTS.md": "# stale\n",
            "pkg/CLAUDE.md": "# pkg\n",
        },
    )
    assert _run(root, fix=True) == []
    assert (root / "AGENTS.md").read_text() == sync.render()
    assert (root / "pkg/AGENTS.md").read_text() == sync.render()
    assert (root / "CLAUDE.md").read_text() == "# guide\n"
    assert _run(root) == []


def test_fix_never_writes_through_a_symlinked_pointer(tmp_path: Path) -> None:
    """A tracked AGENTS.md symlink must not let --fix overwrite its target."""
    outside = tmp_path / "outside.txt"
    outside.write_text("not ours\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    root = _repo(repo, {"CLAUDE.md": "# guide\n"})
    (root / "AGENTS.md").symlink_to(outside)
    subprocess.run(["git", "add", "AGENTS.md"], cwd=root, check=True)

    problems = _run(root, fix=True)

    assert outside.read_text() == "not ours\n"
    assert problems == [
        "AGENTS.md: is a symlink or resolves outside the repository; replace it "
        "with a regular file (--fix will not write through it)"
    ]


def test_a_symlink_inside_the_repository_is_refused_too(tmp_path: Path) -> None:
    """Even pointing at its own CLAUDE.md: a write would clobber the guide."""
    root = _repo(tmp_path, {"CLAUDE.md": "# guide\n"})
    (root / "AGENTS.md").symlink_to("CLAUDE.md")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=root, check=True)

    assert len(_run(root, fix=True)) == 1
    assert (root / "CLAUDE.md").read_text() == "# guide\n"


@pytest.mark.parametrize(("argv", "code"), [([], 1), (["--fix"], 0)])
def test_main_exit_codes(
    tmp_path: Path, argv: list[str], code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"CLAUDE.md": "# guide\n", "AGENTS.md": "# stale\n"})
    assert sync.main([*argv, "--root", str(root)]) == code
    out = capsys.readouterr().out
    assert ("--fix" in out) is (code == 1)
