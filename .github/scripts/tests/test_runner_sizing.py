"""Every ``ubuntu-slim`` job fits what that runner provides (FND-3335).

``ubuntu-slim`` is a third of the price of ``ubuntu-latest`` per billed minute,
which is why short glue jobs moved to it. It is also a different machine: an
unprivileged container, not a VM, on a minimal image. A job that silently
depended on the full image breaks there, and because every connector calls the
reusables @main, it breaks across the fleet at once. These tests pin the
constraints written down in docs/standards/ci.md ("Runner sizing"):

* GitHub kills a slim job at 15 minutes, whatever it declares, so each one
  declares a ``timeout-minutes`` of 15 or less and the cap is visible;
* no Docker daemon, so no ``services:``, no ``container:``, no ``docker`` step;
* no ``sudo`` / ``apt-get``: unverified inside the unprivileged container;
* every ``uses:`` is on an allowlist of actions checked to be JavaScript or
  stdlib-Python composites (a Docker action cannot run there);
* every script run with the system ``python3`` imports the stdlib only. The
  full image's system Python carries apt modules (PyYAML, packaging) that the
  slim one does not, so a script that leaned on them would pass on
  ``ubuntu-latest`` and fail on ``ubuntu-slim``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIRS = (
    REPO_ROOT / ".github" / "workflows",
    REPO_ROOT / "packages" / "conformance" / "conformance" / "bootstrap" / "templates",
)
SCRIPT_DIRS = (
    REPO_ROOT / ".github" / "scripts",
    REPO_ROOT / ".github" / "actions",
    REPO_ROOT / "packages" / "conformance" / "conformance" / "bootstrap" / "templates",
)

SLIM = "ubuntu-slim"
SLIM_MAX_TIMEOUT_MINUTES = 15

#: Actions a slim job may use, each checked at its pinned SHA to be
#: `runs.using: node24` — or, for the local ones, a composite that runs a
#: stdlib-only Python script (enforced below). Add to this list only after the
#: same check: a Docker action needs the daemon slim does not have.
SLIM_ACTIONS = frozenset(
    {
        "actions/checkout",
        "actions/create-github-app-token",
        "actions/download-artifact",
        "actions/github-script",
        "actions/stale",
        "actions/upload-artifact",
        "astral-sh/setup-uv",
        "aws-actions/configure-aws-credentials",
        "github/codeql-action/upload-sarif",
        "marocchino/sticky-pull-request-comment",
        "mshick/add-pr-comment",
        "peter-evans/repository-dispatch",
        "softprops/action-gh-release",
        "atlanhq/application-sdk/.github/actions/discover-e2e-suites",
        "atlanhq/application-sdk/.github/actions/e2e-dispatch-recheck",
        "atlanhq/application-sdk/.github/actions/verify-test-gate",
    }
)

#: Local composites on the allowlist, by directory: their Python must be
#: stdlib-only too. Sorted: a frozenset's order differs per xdist worker.
SLIM_LOCAL_ACTION_DIRS = tuple(
    REPO_ROOT / ".github" / "actions" / a.rsplit("/", 1)[-1]
    for a in sorted(SLIM_ACTIONS)
    if a.startswith("atlanhq/application-sdk/.github/actions/")
)

_PRIVILEGED = re.compile(r"(^|[\s;&|(])(sudo|apt-get|docker)\s", re.M)
# The SYSTEM python, not a venv's (`/tmp/venv/bin/python x.py` is excluded by
# the lookbehind): that is the interpreter whose site-packages differs. Bare
# `python` counts too: on a runner that has it, it is the same system Python.
_SYSTEM_PY_SCRIPT = re.compile(r"(?<![/\w])python3?\s+(?:-\S+\s+)*([\w./${}-]+\.py)")
_INLINE_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", re.M)


def _slim_jobs() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for root in WORKFLOW_DIRS:
        for path in sorted(root.glob("*.y*ml")):
            text = path.read_text(encoding="utf-8")
            try:
                workflow = yaml.safe_load(text)
            except yaml.YAMLError:
                # A jinja template that only renders: it may not hide a slim
                # job from this check.
                assert SLIM not in text, f"{path.name}: unparseable and uses {SLIM}"
                continue
            if not isinstance(workflow, dict):
                continue
            for job_id, job in (workflow.get("jobs") or {}).items():
                if job.get("runs-on") == SLIM:
                    yield path.name, job_id, job


SLIM_JOBS = list(_slim_jobs())
_IDS = [f"{name}:{job_id}" for name, job_id, _ in SLIM_JOBS]


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _find_script(ref: str) -> Path:
    name = ref.rsplit("/", 1)[-1]
    for root in SCRIPT_DIRS:
        hits = sorted(root.rglob(name))
        if hits:
            return hits[0]
    raise AssertionError(f"script {ref} not found under {SCRIPT_DIRS}")


def _third_party_imports(path: Path, seen: set[Path]) -> set[str]:
    """Top-level imports of *path* that are neither stdlib nor a sibling script."""
    if path in seen:
        return set()
    seen.add(path)
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top == "__future__":
                continue
            sibling = path.parent / f"{top}.py"
            if sibling.exists():
                out |= _third_party_imports(sibling, seen)
            else:
                out.add(top)
    return out


def test_some_jobs_run_on_slim() -> None:
    # Guards the guard: a parse change that found no slim jobs would make
    # every parametrized test below vacuously pass.
    assert len(SLIM_JOBS) >= 20


@pytest.mark.parametrize(("name", "job_id", "job"), SLIM_JOBS, ids=_IDS)
def test_declares_timeout_within_slim_cap(
    name: str, job_id: str, job: dict[str, Any]
) -> None:
    timeout = job.get("timeout-minutes")
    assert isinstance(timeout, int), f"{name}:{job_id} must declare timeout-minutes"
    assert timeout <= SLIM_MAX_TIMEOUT_MINUTES


@pytest.mark.parametrize(("name", "job_id", "job"), SLIM_JOBS, ids=_IDS)
def test_needs_no_docker_or_root(name: str, job_id: str, job: dict[str, Any]) -> None:
    assert "services" not in job
    assert "container" not in job
    assert not _PRIVILEGED.search(_run_text(job)), f"{name}:{job_id}"


@pytest.mark.parametrize(("name", "job_id", "job"), SLIM_JOBS, ids=_IDS)
def test_uses_only_allowlisted_actions(
    name: str, job_id: str, job: dict[str, Any]
) -> None:
    for step in job.get("steps", []):
        if "uses" in step:
            action = step["uses"].split("@", 1)[0]
            assert action in SLIM_ACTIONS, f"{name}:{job_id} uses {action}"


@pytest.mark.parametrize(("name", "job_id", "job"), SLIM_JOBS, ids=_IDS)
def test_system_python_is_stdlib_only(
    name: str, job_id: str, job: dict[str, Any]
) -> None:
    run = _run_text(job)
    for ref in _SYSTEM_PY_SCRIPT.findall(run):
        script = _find_script(ref)
        assert not _third_party_imports(script, set()), f"{name}:{job_id} {ref}"
    if "python" in run:
        inline = set(_INLINE_IMPORT.findall(run)) - set(sys.stdlib_module_names)
        assert not inline, f"{name}:{job_id} inline python imports {inline}"


@pytest.mark.parametrize("action_dir", SLIM_LOCAL_ACTION_DIRS, ids=lambda p: p.name)
def test_allowlisted_local_actions_are_stdlib_only(action_dir: Path) -> None:
    scripts = sorted(action_dir.glob("*.py"))
    assert scripts, action_dir
    for script in scripts:
        assert not _third_party_imports(script, set()), script


def test_full_image_python_dependency_is_caught() -> None:
    # The trap this file exists for, kept red: build-and-publish-app's
    # `prepare` runs validate_atlan_yaml.py on the system python3, which needs
    # PyYAML and packaging from the full image. It stays on ubuntu-latest.
    script = REPO_ROOT / ".github" / "scripts" / "validate_atlan_yaml.py"
    assert _third_party_imports(script, set()) >= {"yaml"}
