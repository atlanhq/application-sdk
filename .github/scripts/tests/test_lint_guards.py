"""Guards for the lint toolchain (FND-4247).

Two failures this pins shut:

1. **Local and CI lint disagree.** The pre-commit hooks and the dev group used
   to pin different versions of the same tool (isort 5.13.2 in the hook, 9.0.2
   in the lock), so `uv run isort` rewrote 16 files CI accepted. isort is gone;
   ruff now sorts imports, and its three pins (the ruff-pre-commit hook rev, the
   root dev group, packages/conformance's dev group) must be one version.
   Renovate bumps them in a single grouped PR; this test is what fails a PR
   that moves one without the others.

2. **The required `Pre-commit Checks` job runs with the default token.** It
   reads the tree and nothing else, so it gets `contents: read` and no more.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_CODEQL = _REPO_ROOT / ".github/workflows/codeql.yaml"
_RENOVATE = _REPO_ROOT / "renovate.json"
_PYPROJECTS = (
    _REPO_ROOT / "pyproject.toml",
    _REPO_ROOT / "packages/conformance/pyproject.toml",
)

_RUFF_HOOKS_REPO = "https://github.com/astral-sh/ruff-pre-commit"
_RUFF_PIN_RE = re.compile(r"^ruff==(?P<version>\S+)$")


def _hook_repos() -> list[dict]:
    return yaml.safe_load(_PRE_COMMIT.read_text())["repos"]


def ruff_hook_version() -> str:
    for repo in _hook_repos():
        if repo.get("repo") == _RUFF_HOOKS_REPO:
            return str(repo["rev"]).removeprefix("v")
    raise AssertionError(f"{_RUFF_HOOKS_REPO} is not in .pre-commit-config.yaml")


def ruff_dev_pin(pyproject: Path) -> str:
    dev = tomllib.loads(pyproject.read_text())["dependency-groups"]["dev"]
    for requirement in dev:
        if isinstance(requirement, str) and (match := _RUFF_PIN_RE.match(requirement)):
            return match.group("version")
    raise AssertionError(
        f"{pyproject.relative_to(_REPO_ROOT)} has no exact `ruff==` dev pin, "
        "so `uv run ruff` would not match the pre-commit hook"
    )


class TestRuffPinParity:
    def test_every_ruff_pin_is_the_hook_version(self) -> None:
        hook = ruff_hook_version()
        pins = {
            str(path.relative_to(_REPO_ROOT)): ruff_dev_pin(path)
            for path in _PYPROJECTS
        }
        assert set(pins.values()) == {hook}, (
            f"ruff-pre-commit is at {hook} but the dev pins are {pins}: local "
            "`uv run ruff` would lint, format and sort imports differently "
            "from CI. Move all three together (Renovate's `ruff` group does)."
        )

    def test_isort_is_not_a_second_import_sorter(self) -> None:
        # Two sorters with different rules is the drift this issue removed.
        assert all("isort" not in str(repo.get("repo")) for repo in _hook_repos())
        for path in _PYPROJECTS:
            dev = tomllib.loads(path.read_text())["dependency-groups"]["dev"]
            assert not any(str(r).startswith("isort") for r in dev), path

    def test_ruff_sorts_imports_under_every_config(self) -> None:
        # packages/conformance/pyproject.toml is the nearest ruff config for
        # that package, so the root's rule set never reaches it: both must
        # select `I` themselves.
        for path in _PYPROJECTS:
            lint = tomllib.loads(path.read_text())["tool"]["ruff"]["lint"]
            assert "I" in lint.get("select", []), path

    def test_renovate_groups_the_ruff_pins(self) -> None:
        rules = yaml.safe_load(_RENOVATE.read_text())["packageRules"]
        grouped = [
            rule
            for rule in rules
            if rule.get("groupName") == "ruff"
            and {"ruff", "astral-sh/ruff-pre-commit"}
            <= set(rule.get("matchPackageNames", []))
        ]
        assert grouped, "the ruff pins must share one Renovate group"

    def test_renovate_pre_commit_manager_is_enabled(self) -> None:
        config = yaml.safe_load(_RENOVATE.read_text())
        assert config.get("pre-commit", {}).get("enabled") is True


class TestPreCommitJobPermissions:
    def test_lint_job_is_read_only(self) -> None:
        job = yaml.safe_load(_CODEQL.read_text())["jobs"]["lint"]
        assert job["name"] == "Pre-commit Checks", (
            "a required status check: renaming the job changes its context"
        )
        assert job.get("permissions") == {"contents": "read"}

    def test_workflow_default_grants_nothing(self) -> None:
        workflow = yaml.safe_load(_CODEQL.read_text())
        assert workflow.get("permissions") == {}
