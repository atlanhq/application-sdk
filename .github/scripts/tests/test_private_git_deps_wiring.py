"""Wiring of the private-git-dependency token in the CI reusables (FND-4740).

An app that pins a PRIVATE atlanhq git dependency needs a GitHub token for the
``uv sync`` / ``uv lock`` that clones it. These jobs used to write that token
(the org-admin ORG_PAT_GITHUB) into ``~/.gitconfig`` and expose it as job-level
env, so every later step, including the tests, pre-commit hooks and
conformance series that import app code, could read it.

They now mint a read-only fleet App token and hand it to the fetching command
alone, through ``.github/scripts/git_auth.py`` (scoped ``GIT_CONFIG_*`` env,
nothing persisted). These tests pin that shape, so a later edit cannot
quietly put the token back in reach of app code:

* no step writes git config, and no job exposes a token as job-level env;
* the token is minted read-only, from the App, never from ORG_PAT_GITHUB;
* it is read only by the guard and by steps that run it through git_auth.py
  (directly, or via a composite's ``git-auth-token`` input), and inside the
  composites only by git_auth.py steps;
* a missing token fails with an error naming the secrets.

The protected behaviour (what a child process can see) is GitHub Actions' and
git's own; git_auth.py itself is executed in test_git_auth.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

Workflow = dict[Any, Any]
Step = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONS = REPO_ROOT / ".github" / "actions"

#: (workflow file, job id, the boolean input that opts a caller in)
JOBS = [
    ("tests-reusable.yaml", "unit", "private-git-deps"),
    ("tests-reusable.yaml", "integration", "private-git-deps"),
    ("tests-reusable.yaml", "e2e", "private-git-deps"),
    ("checks-reusable.yaml", "pre-commit", "private-git-deps"),
    ("conformance-reusable.yaml", "suite", "private-git-deps"),
    ("release-version-bump.yaml", "bump-version", "private_git_auth"),
]
JOB_IDS = [f"{wf}:{job}" for wf, job, _ in JOBS]

#: Composites that receive the token as ``git-auth-token``.
COMPOSITES = [
    "setup-deps",
    "connector-unit-tests",
    "connector-integration-tests",
    "sdr-e2e",
]

MINT_ID = "git-deps-token"
TOKEN_REF = f"steps.{MINT_ID}.outputs.token"
APP_TOKEN_ACTION = "actions/create-github-app-token@"
#: ``python3 <path>/git_auth.py run -- <cmd>``; the path may be quoted and hold
#: an expression (``"${{ github.action_path }}/../../scripts/git_auth.py"``).
GIT_AUTH_RUN = re.compile(r'python3 "?[^\n"]*/git_auth\.py"? run -- \S')
GIT_CONFIG_WRITE = re.compile(
    r"git config\b.*(insteadOf|--global|extraheader)|insteadOf"
)


def _load(path: Path) -> Workflow:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job(workflow_file: str, job: str) -> dict[str, Any]:
    return _load(WORKFLOWS / workflow_file)["jobs"][job]


def _steps(workflow_file: str, job: str) -> list[Step]:
    return _job(workflow_file, job)["steps"]


def _mint(steps: list[Step]) -> Step:
    mints = [s for s in steps if s.get("id") == MINT_ID]
    assert len(mints) == 1, f"expected one step with id {MINT_ID!r}"
    return mints[0]


def _runs_git_auth(step: Step) -> bool:
    return bool(GIT_AUTH_RUN.search(step.get("run", "")))


# ── No persisted credential, no job-wide token ────────────────────────────────


@pytest.mark.parametrize(("workflow_file", "job", "flag"), JOBS, ids=JOB_IDS)
def test_no_step_writes_a_git_credential(
    workflow_file: str, job: str, flag: str
) -> None:
    writers = [
        s.get("name")
        for s in _steps(workflow_file, job)
        if GIT_CONFIG_WRITE.search(s.get("run", ""))
    ]
    assert writers == [], (
        "git config is readable by every later step, including app code"
    )


@pytest.mark.parametrize(("workflow_file", "job", "flag"), JOBS, ids=JOB_IDS)
def test_no_job_level_token(workflow_file: str, job: str, flag: str) -> None:
    env = _job(workflow_file, job).get("env") or {}
    assert "GIT_TOKEN" not in env
    leaked = [
        k for k, v in env.items() if "ORG_PAT_GITHUB" in str(v) or TOKEN_REF in str(v)
    ]
    assert leaked == [], "job-level env reaches every step"


# ── The mint ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("workflow_file", "job", "flag"), JOBS, ids=JOB_IDS)
def test_token_is_a_read_only_app_token(
    workflow_file: str, job: str, flag: str
) -> None:
    mint = _mint(_steps(workflow_file, job))
    assert mint["uses"].startswith(APP_TOKEN_ACTION)
    assert mint["if"] == f"inputs.{flag}"
    # A missing secret must reach the named guard below, not fail bare here.
    assert mint.get("continue-on-error") is True
    with_ = mint["with"]
    assert with_["permission-contents"] == "read"
    assert with_["owner"] == "atlanhq"
    # Org-wide: the dependency lives in another repo.
    assert "repositories" not in with_
    assert (
        "FLEET_APP_ID" in with_["app-id"]
        and "FLEET_APP_PRIVATE_KEY" in with_["private-key"]
    )


@pytest.mark.parametrize(("workflow_file", "job", "flag"), JOBS, ids=JOB_IDS)
def test_missing_token_fails_naming_the_secrets(
    workflow_file: str, job: str, flag: str
) -> None:
    steps = _steps(workflow_file, job)
    mint = steps.index(_mint(steps))
    guards = [
        (i, s)
        for i, s in enumerate(steps)
        if s.get("if") == f"inputs.{flag} && {TOKEN_REF} == ''"
    ]
    assert len(guards) == 1
    i, guard = guards[0]
    assert i == mint + 1
    assert "exit 1" in guard["run"]
    assert "FLEET_APP_ID" in guard["run"] and "FLEET_APP_PRIVATE_KEY" in guard["run"]


# ── Who reads the token ───────────────────────────────────────────────────────


def _readers(steps: list[Step]) -> list[tuple[Step, str]]:
    """(step, where) for every place a step reads the minted token."""
    found: list[tuple[Step, str]] = []
    for step in steps:
        for key, value in (step.get("env") or {}).items():
            if TOKEN_REF in str(value):
                found.append((step, f"env.{key}"))
        for key, value in (step.get("with") or {}).items():
            if TOKEN_REF in str(value):
                found.append((step, f"with.{key}"))
        if TOKEN_REF in step.get("run", ""):
            found.append((step, "run"))
    return found


@pytest.mark.parametrize(("workflow_file", "job", "flag"), JOBS, ids=JOB_IDS)
def test_token_reaches_only_git_auth(workflow_file: str, job: str, flag: str) -> None:
    readers = _readers(_steps(workflow_file, job))
    assert readers, "the minted token is never used"
    for step, where in readers:
        name = step.get("name") or step.get("uses")
        if where == "env.GIT_TOKEN":
            assert _runs_git_auth(step), (
                f"{name}: GIT_TOKEN on a step that does not run git_auth.py"
            )
        elif where == "with.git-auth-token":
            assert step["uses"].startswith(
                "atlanhq/application-sdk/.github/actions/"
            ), name
            assert step["uses"].split("/")[4].split("@")[0] in COMPOSITES, name
        else:
            pytest.fail(f"{name}: token read as {where}")


@pytest.mark.parametrize(("workflow_file", "job", "flag"), JOBS, ids=JOB_IDS)
def test_git_auth_steps_require_the_token_only_when_opted_in(
    workflow_file: str, job: str, flag: str
) -> None:
    for step, where in _readers(_steps(workflow_file, job)):
        if where != "env.GIT_TOKEN":
            continue
        required = str(step["env"]["PRIVATE_GIT_DEPS"])
        assert required in {"true", f"${{{{ inputs.{flag} }}}}"}, step.get("name")
        if required == "true":
            assert f"inputs.{flag}" in step["if"], step.get("name")


@pytest.mark.parametrize(
    ("workflow_file", "job", "flag"),
    [j for j in JOBS if j[0] != "release-version-bump.yaml"],
    ids=[i for i in JOB_IDS if not i.startswith("release")],
)
def test_composite_gets_the_token_only_when_opted_in(
    workflow_file: str, job: str, flag: str
) -> None:
    for step, where in _readers(_steps(workflow_file, job)):
        if where == "with.git-auth-token":
            assert (
                step["with"]["git-auth-token"]
                == f"${{{{ inputs.{flag} && {TOKEN_REF} || '' }}}}"
            )


# ── Inside the composites ─────────────────────────────────────────────────────


@pytest.mark.parametrize("action", COMPOSITES)
def test_composite_hands_the_token_only_to_git_auth(action: str) -> None:
    spec = _load(ACTIONS / action / "action.yaml")
    assert spec["inputs"]["git-auth-token"]["default"] == ""
    steps = spec["runs"]["steps"]
    readers = 0
    for step in steps:
        assert not GIT_CONFIG_WRITE.search(step.get("run", "")), step.get("name")
        env = step.get("env") or {}
        with_ = step.get("with") or {}
        if "inputs.git-auth-token" in str(env):
            assert list(
                k for k, v in env.items() if "inputs.git-auth-token" in str(v)
            ) == ["GIT_TOKEN"]
            assert _runs_git_auth(step), step.get("name")
            readers += 1
        if "inputs.git-auth-token" in str(with_):
            assert step["uses"].startswith(
                "atlanhq/application-sdk/.github/actions/setup-deps@"
            )
            readers += 1
        assert "inputs.git-auth-token" not in step.get("run", ""), step.get("name")
    assert readers, f"{action} never uses git-auth-token"


def test_setup_deps_tokenless_install_is_unchanged() -> None:
    """A caller without a token installs exactly as before (and never needs python3)."""
    steps = _load(ACTIONS / "setup-deps" / "action.yaml")["runs"]["steps"]
    by_name = {s.get("name"): s for s in steps}
    plain = by_name["Install dependencies"]
    authed = by_name["Install dependencies (private git dependencies)"]
    assert (
        plain["if"]
        == "steps.cache-venv.outputs.cache-hit != 'true' && inputs.git-auth-token == ''"
    )
    assert "git_auth.py" not in plain["run"]
    assert (
        authed["if"]
        == "steps.cache-venv.outputs.cache-hit != 'true' && inputs.git-auth-token != ''"
    )
    assert "uv sync --all-extras --all-groups" in authed["run"]


# ── Conformance: the vendored detect action never sees the token ─────────────


def test_conformance_presyncs_before_the_dependency_series() -> None:
    steps = _steps("conformance-reusable.yaml", "suite")
    ids = [s.get("id") for s in steps]
    presync = ids.index("git-deps-presync")
    detect = ids.index("detect-dependency")
    assert presync < detect
    # Same plain sync the vendored action runs, so its own sync fetches nothing.
    assert steps[presync]["run"].endswith("run -- uv sync")
    # A failed pre-sync stops D rather than letting it fail again, unexplained.
    assert "steps.git-deps-presync.outcome != 'failure'" in steps[detect]["if"]
    assert "GIT_TOKEN" not in str(steps[detect].get("env") or {})
    assert "git-auth" not in str(steps[detect].get("with") or {})


# ── Interface: callers pin @main and must keep parsing ───────────────────────


@pytest.mark.parametrize(
    "workflow_file",
    [
        "tests-reusable.yaml",
        "checks-reusable.yaml",
        "conformance-reusable.yaml",
        "release-version-bump.yaml",
    ],
)
def test_secrets_interface_keeps_org_pat_and_declares_the_app(
    workflow_file: str,
) -> None:
    on = _load(WORKFLOWS / workflow_file)[True]  # YAML 1.1 reads `on:` as True
    secrets = on["workflow_call"]["secrets"]
    # Still declared: a caller that maps it explicitly would fail at startup.
    assert "ORG_PAT_GITHUB" in secrets
    for name in ("FLEET_APP_ID", "FLEET_APP_PRIVATE_KEY"):
        assert secrets[name]["required"] is False
