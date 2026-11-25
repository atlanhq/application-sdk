"""The repo's agent skills point at files that exist, and have one copy each.

A skill is read by an agent that trusts it: a path it names that no longer
exists sends the agent looking for code that moved or was deleted, and a
skill kept as two hand-synced copies drifts until the agent reads whichever
copy is stale.

Only paths under roots that exist solely in this repo are checked. Many
skills (``typed-failures``, ``upgrade-v3``) describe a *connector app*, whose
``tests/``, ``app/`` and ``contract/`` paths are relative to that app and
are not expected to resolve here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SKILL_DIRS = (_REPO / ".claude" / "skills", _REPO / ".agents" / "skills")

# Top-level directories a connector app does not have, so a path under one
# can only mean this repo. Allowlist, not denylist: a root missing here is
# just unchecked, never a false failure.
_REPO_ONLY_ROOTS = frozenset(
    {
        ".agents",
        ".claude",
        "application_sdk",
        "contract-toolkit",
        "docs",
        "packages",
        "tools",
    }
)

_BACKTICKED = re.compile(r"`([^`\s]+)`")
# A template path such as `application_sdk/<area>/errors.py` names a shape,
# not a file.
_PLACEHOLDER = re.compile(r"[<>{}*$]")


def _skill_markdown_files() -> list[Path]:
    """Every .md file under the skill dirs, not descending into linked dirs.

    A symlinked skill directory is skipped: the ``.agents`` links point at
    ``.claude/skills``, which is walked anyway, and the conformance skills
    point into ``packages/conformance``, whose content ships to connector
    apps and names paths relative to them. A symlinked *file* (the remediate
    skill, linked to the bootstrap template) is read through its link: it is
    this repo's skill, so the paths it names must resolve here too.
    """
    files: list[Path] = []
    for skill_dir in _SKILL_DIRS:
        for dirpath, dirnames, filenames in os.walk(skill_dir):
            base = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
            files.extend(base / name for name in filenames if name.endswith(".md"))
    return sorted(files)


def _repo_path(token: str) -> str | None:
    """The repo-relative path a backticked token names, or None if it is not one."""
    token = token.rstrip(".,;:)")
    if "/" not in token or _PLACEHOLDER.search(token):
        return None
    if token.split("/", 1)[0] not in _REPO_ONLY_ROOTS:
        return None
    # `path/to/file.py:42` and `docs/page.md#anchor` name the file.
    return token.split("#", 1)[0].split(":", 1)[0]


def _dead_paths() -> list[str]:
    dead: list[str] = []
    for path in _skill_markdown_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _BACKTICKED.finditer(line):
                target = _repo_path(match.group(1))
                if target is not None and not (_REPO / target).exists():
                    rel = path.relative_to(_REPO).as_posix()
                    dead.append(f"{rel}:{lineno}: {target}")
    return dead


def test_skill_files_are_found() -> None:
    # Guards the walk itself: a path change that finds no skills would
    # make the resolution test pass vacuously.
    files = _skill_markdown_files()
    assert len(files) > 10
    assert _REPO / ".claude" / "skills" / "remediate" / "SKILL.md" in files


def test_every_backticked_repo_path_in_a_skill_resolves() -> None:
    dead = _dead_paths()
    assert not dead, (
        "Skills name repo paths that do not exist. Point each at the file's "
        "current location, or drop it if the file is gone:\n  " + "\n  ".join(dead)
    )


def test_repo_path_extraction() -> None:
    assert _repo_path("application_sdk/clients/sql.py:42") == (
        "application_sdk/clients/sql.py"
    )
    assert _repo_path("docs/guide.md#section") == "docs/guide.md"
    assert _repo_path("application_sdk/<area>/errors.py") is None
    assert _repo_path("tests/unit/test_failures.py") is None
    assert _repo_path("app/failures.py") is None
    assert _repo_path("application_sdk") is None


def test_agents_skills_are_symlinks_into_claude_skills() -> None:
    agents = _REPO / ".agents" / "skills"
    claude = (_REPO / ".claude" / "skills").resolve()
    copies = sorted(p.name for p in agents.iterdir() if not p.is_symlink())
    assert not copies, (
        f".agents/skills holds hand-kept copies {copies}; replace each with a "
        "symlink to ../../.claude/skills/<name> so there is one source."
    )
    for link in agents.iterdir():
        target = link.resolve()
        assert target == claude / link.name and target.is_dir(), (
            f"{link.relative_to(_REPO)} -> {os.readlink(link)} does not resolve "
            f"to .claude/skills/{link.name}"
        )


def test_root_remediate_skill_is_the_bootstrap_template() -> None:
    # The SDK repo's /remediate and the copy bootstrap writes into connector
    # apps are one file; packages/conformance/tests/test_remediate_template_renders_verbatim.py
    # holds that the template renders to itself, so this symlink reads
    # exactly what a connector app gets.
    skill = _REPO / ".claude" / "skills" / "remediate" / "SKILL.md"
    template = (
        _REPO
        / "packages"
        / "conformance"
        / "conformance"
        / "bootstrap"
        / "templates"
        / "remediate.md"
    )
    assert skill.is_symlink()
    assert skill.resolve() == template.resolve()
