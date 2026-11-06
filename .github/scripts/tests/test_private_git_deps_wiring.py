"""Wiring of the private-git-dependency token in the CI reusables (FND-4740).

An app that pins a PRIVATE atlanhq git dependency needs a GitHub token for the
``uv sync`` / ``uv lock`` that clones it. These jobs used to write that token
(the org-admin ORG_PAT_GITHUB) into ``~/.gitconfig`` and expose it as job-level
env, so every later step, including the tests, pre-commit hooks and
conformance series that import app code, could read it.

They now mint a read-only fleet App token scoped to the app's private repos
(the inline shape pinned by ``_scoped_mint.py``) and hand it to
``.github/scripts/git_auth.py`` alone, which uses it only to mirror-clone those
repos: uv, and every build backend it runs, install from the local mirrors
with no token in their env (lens round 5). These tests pin that shape, so a
later edit cannot quietly put the token back in reach of app code:

* no step writes git config, and no job exposes a token as job-level env;
* the token is minted read-only, from the App, scoped to the repos the app
  names, never unscoped and never from ORG_PAT_GITHUB;
* it is read only by the guard and by steps that run it through git_auth.py
  (directly, or via a composite's ``git-auth-token`` input), and inside the
  composites only by git_auth.py steps;
* a missing token fails with an error naming the secrets.

The protected behaviour (what a child process can see) is GitHub Actions' and
git's own; git_auth.py itself is executed in test_git_auth.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scoped_mint import assert_scoped_mint  # noqa: E402

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
REPOS_ID = "git-deps-repos"
REPOS_REF = f"steps.{REPOS_ID}.outputs.repositories"
#: ``python3 <path>/git_auth.py run -- <cmd>``; the path may be quoted and hold
#: an expression (``"${{ github.action_path }}/../../scripts/git_auth.py"``).
GIT_AUTH_RUN = re.compile(r'python3 "?[^\n"]*/git_auth\.py"? run -- \S')
#: ``python3 <path>/git_auth.py mirror``: the only kind of step that may hold the
#: token. It exits before uv runs, so no ancestor of a build backend has it.
GIT_AUTH_MIRROR = re.compile(r'python3 "?[^\n"]*/git_auth\.py"? mirror\s*$')
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


def _mirrors(step: Step) -> bool:
    return bool(GIT_AUTH_MIRROR.search(step.get("run", "")))


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
def test_token_is_minted_scoped_to_the_apps_private_repos(
    workflow_file: str, job: str, flag: str
) -> None:
    assert_scoped_mint(
        _steps(workflow_file, job),
        mint_id=MINT_ID,
        repos_id=REPOS_ID,
        cond=f"inputs.{flag}",
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
        if s.get("if") == f"inputs.{flag} && {REPOS_REF} != '' && {TOKEN_REF} == ''"
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
            assert _mirrors(step), (
                f"{name}: GIT_TOKEN on a step other than `git_auth.py mirror`"
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
            assert _mirrors(step), step.get("name")
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


# ── Release bump: callers pinned to an older sdk_scripts_ref ─────────────────


def test_release_bump_relocks_without_git_auth_when_not_opted_in() -> None:
    """A caller pinning ``sdk_scripts_ref`` to a revision without git_auth.py
    must still bump: the no-private-deps path never runs it."""
    steps = _steps("release-version-bump.yaml", "bump-version")
    relocks = [s for s in steps if "uv lock" in s.get("run", "")]
    assert [s["if"] for s in relocks] == [
        "${{ !inputs.private_git_auth }}",
        "inputs.private_git_auth",
    ]
    plain, authed = relocks
    assert plain["run"].strip() == "uv lock"
    assert "env" not in plain
    assert _runs_git_auth(authed)


# ── The token never shares a step with uv (lens round 6) ─────────────────────


def _all_steps() -> list[tuple[str, Step]]:
    found: list[tuple[str, Step]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        jobs: dict[str, dict[str, Any]] = _load(path).get("jobs") or {}
        for job_id, job in jobs.items():
            for step in job.get("steps") or []:
                found.append((f"{path.name}:{job_id}", step))
    for path in sorted(ACTIONS.glob("*/action.y*ml")):
        runs: dict[str, Any] = _load(path).get("runs") or {}
        for step in runs.get("steps") or []:
            found.append((path.parent.name, step))
    return found


def test_no_step_that_runs_uv_through_git_auth_holds_a_token() -> None:
    """A build backend can read /proc/<pid>/environ of every ancestor, so the
    step that starts uv (``git_auth.py run``, certify's ``install-deps``) must
    have no token in its env; ``git_auth.py run`` refuses one too."""
    offenders = [
        f"{where}: {step.get('name')}"
        for where, step in _all_steps()
        if (_runs_git_auth(step) or "certify.py install-deps" in step.get("run", ""))
        and (
            "GIT_TOKEN" in (step.get("env") or {})
            or "token" in str(step.get("env") or {}).lower()
        )
    ]
    assert offenders == []


def test_every_token_holding_step_only_mirrors() -> None:
    """Wherever GIT_TOKEN is set, the step runs ``git_auth.py mirror`` and
    nothing else, or is an image build handing it to BuildKit."""
    for where, step in _all_steps():
        if "GIT_TOKEN" not in (step.get("env") or {}):
            continue
        run = step.get("run", "")
        if "--secret" in run or "build-app-image" in where:
            continue  # BuildKit secret (test_image_build_git_token.py)
        assert _mirrors(step), f"{where}: {step.get('name')}"
        assert run.strip().count("\n") == 0, f"{where}: mirror step does more"


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
