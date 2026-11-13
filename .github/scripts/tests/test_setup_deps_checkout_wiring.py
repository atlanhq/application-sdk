"""setup-deps never leaves the job token on disk, and skips a redundant checkout (FND-4900).

``setup-deps`` used to open with an ``actions/checkout`` that set no
``persist-credentials``, so it wrote the job token into ``.git/config``. Every
in-repo caller had already checked the repo out, several with
``persist-credentials: false``, and the unit, integration and e2e tiers run PR
code after ``setup-deps``: the second checkout put back the token the first
one had kept out. Its ``git clean`` also wiped the workspace, which is what
``sdr-e2e``'s stash / action-root / restore-assets workarounds existed for.

Now both checkouts in ``setup-deps`` set ``persist-credentials: false``, and an
input ``checkout`` (default ``"true"``, so fleet callers that rely on it are
unchanged) lets a job that already checked out skip it. These tests pin:

* the action shape (both checkouts, the input, the LFS exception);
* the pass-through in the connector test actions;
* the call-site rule, across every workflow job: a job that checks the repo
  out (unconditionally, at the workspace root) before calling ``setup-deps``
  or a composite that wraps it passes ``checkout: "false"``, and a job that
  passes it has such a checkout;
* ``sdr-e2e``'s replacement for the wipe, which is EXECUTED in a scratch repo.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB = REPO_ROOT / ".github"
ACTIONS = GITHUB / "actions"
WORKFLOWS = GITHUB / "workflows"
SETUP_DEPS = ACTIONS / "setup-deps" / "action.yaml"
SDR_E2E = ACTIONS / "sdr-e2e" / "action.yaml"

CHECKOUT_ACTION = "actions/checkout@"
RESTORE_STEP = "Restore the checked-out tree"

# The composites whose `checkout` input reaches setup-deps' own. A call to any
# of these, or to setup-deps itself, is governed by the call-site rule.
PASS_THROUGH = ("connector-unit-tests", "connector-integration-tests")
GOVERNED = ("setup-deps", *PASS_THROUGH)

Step = dict[str, Any]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _action_steps(path: Path) -> list[Step]:
    return _load(path)["runs"]["steps"]


def _is_checkout(step: Step) -> bool:
    return str(step.get("uses", "")).startswith(CHECKOUT_ACTION)


def _calls(step: Step, action: str) -> bool:
    uses = str(step.get("uses", ""))
    return uses in (
        f"./.github/actions/{action}",
        f"atlanhq/application-sdk/.github/actions/{action}@main",
    )


def _with(step: Step) -> dict[str, Any]:
    return step.get("with") or {}


def _checkout_input(step: Step) -> str:
    return str(_with(step).get("checkout", "true"))


def _checks_out_the_caller_repo(step: Step) -> bool:
    """A checkout of this repository, at the workspace root, on every run."""
    params = _with(step)
    return (
        _is_checkout(step)
        and "if" not in step
        and "repository" not in params
        and "path" not in params
    )


# ── setup-deps itself ────────────────────────────────────────────────────────


def test_setup_deps_checkouts_never_persist_the_token() -> None:
    checkouts = [s for s in _action_steps(SETUP_DEPS) if _is_checkout(s)]

    assert len(checkouts) == 2, "expected the plain and the LFS checkout"
    for step in checkouts:
        assert _with(step).get("persist-credentials") is False, (
            "a setup-deps checkout without persist-credentials: false writes the "
            "job token into .git/config, where the PR code the tests run next "
            f"can read it: {step}"
        )


def test_setup_deps_declares_the_checkout_input_defaulting_to_true() -> None:
    spec = _load(SETUP_DEPS)["inputs"]["checkout"]

    assert spec["default"] == "true", (
        "fleet callers that call setup-deps first rely on it checking out"
    )


def test_only_the_plain_checkout_is_skippable() -> None:
    """`checkout: "false"` skips the plain checkout; `lfs: "true"` still checks out.

    The LFS checkout is how the objects are fetched. With no persisted
    credential, a later `git lfs pull` could not authenticate a private repo.
    """
    plain, lfs = [s for s in _action_steps(SETUP_DEPS) if _is_checkout(s)]

    assert "inputs.checkout != 'false'" in plain["if"]
    assert "inputs.lfs != 'true'" in plain["if"]
    assert "checkout" not in lfs["if"].replace("inputs.lfs", "")
    assert _with(lfs).get("lfs") is True


@pytest.mark.parametrize("action", PASS_THROUGH)
def test_connector_actions_forward_the_checkout_input(action: str) -> None:
    path = ACTIONS / action / "action.yaml"
    assert _load(path)["inputs"]["checkout"]["default"] == "true"

    calls = [s for s in _action_steps(path) if _calls(s, "setup-deps")]
    assert len(calls) == 1
    assert _with(calls[0]).get("checkout") == "${{ inputs.checkout }}"


# ── Call sites ───────────────────────────────────────────────────────────────


def _workflow_jobs() -> list[tuple[str, str, list[Step]]]:
    jobs: list[tuple[str, str, list[Step]]] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        workflow_jobs: dict[str, dict[str, Any]] = _load(path).get("jobs") or {}
        for job_id, job in workflow_jobs.items():
            steps: list[Step] = job.get("steps") or []
            if steps:
                jobs.append((path.name, job_id, steps))
    return jobs


def _governed_calls() -> list[tuple[str, str, int, list[Step]]]:
    found: list[tuple[str, str, int, list[Step]]] = []
    for workflow, job_id, steps in _workflow_jobs():
        for i, step in enumerate(steps):
            if any(_calls(step, action) for action in GOVERNED):
                found.append((workflow, job_id, i, steps))
    return found


def test_the_call_site_scan_finds_the_known_callers() -> None:
    """Guards the scan itself: a renamed `uses:` would make the rule vacuous."""
    sites = {(w, j) for w, j, _, _ in _governed_calls()}

    for expected in (
        ("tests-reusable.yaml", "unit"),
        ("tests-reusable.yaml", "integration"),
        ("checks-reusable.yaml", "pre-commit"),
        ("codeql.yaml", "lint"),
        ("sdk-tests-reusable.yaml", "integration"),
    ):
        assert expected in sites, f"no setup-deps call found in {expected}"


@pytest.mark.parametrize(
    ("workflow", "job_id", "index", "steps"),
    [pytest.param(*site, id=f"{site[0]}::{site[1]}") for site in _governed_calls()],
)
def test_a_job_that_checked_out_skips_the_second_checkout(
    workflow: str, job_id: str, index: int, steps: list[Step]
) -> None:
    checked_out = any(_checks_out_the_caller_repo(s) for s in steps[:index])
    passed = _checkout_input(steps[index])

    if checked_out:
        assert passed == "false", (
            f"{workflow}::{job_id} checks the repo out before "
            f'{steps[index]["uses"]}, so pass checkout: "false": a second '
            "checkout costs time and git-cleans what the job did to the tree"
        )
    else:
        assert passed != "false", (
            f'{workflow}::{job_id} passes checkout: "false" but has no '
            "unconditional root checkout before it, so the tests would run "
            "on an empty workspace"
        )


@pytest.mark.parametrize("action", ["unit-tests", "sdr-e2e"])
def test_composites_that_need_a_prior_checkout_skip_setup_deps_checkout(
    action: str,
) -> None:
    """unit-tests runs only after its jobs' checkouts; sdr-e2e builds the
    caller's tree before setup-deps, so a checkout is already required."""
    calls = [
        s
        for s in _action_steps(ACTIONS / action / "action.yaml")
        if _calls(s, "setup-deps")
    ]

    assert len(calls) == 1
    assert _checkout_input(calls[0]) == "false"


@pytest.mark.parametrize(
    "workflow,job_id",
    [("tests-reusable.yaml", "unit"), ("tests-reusable.yaml", "integration")],
)
def test_tests_reusable_checks_the_app_out_on_every_run(
    workflow: str, job_id: str
) -> None:
    """These jobs used to check the app out only for private-git-deps, and
    relied on setup-deps' checkout otherwise."""
    steps = _load(WORKFLOWS / workflow)["jobs"][job_id]["steps"]
    app = [s for s in steps if s.get("name") == "Checkout app"]

    assert len(app) == 1
    assert _checks_out_the_caller_repo(app[0])
    assert _with(app[0]).get("persist-credentials") is False


# ── sdr-e2e: the restore that replaced the wipe ──────────────────────────────


def _sdr_index(steps: list[Step], predicate: Callable[[Step], bool], what: str) -> int:
    for i, step in enumerate(steps):
        if predicate(step):
            return i
    raise AssertionError(f"no {what} step in {SDR_E2E}")


def test_sdr_e2e_restores_the_tree_between_the_build_and_setup_deps() -> None:
    steps = _action_steps(SDR_E2E)
    build = _sdr_index(steps, lambda s: _calls(s, "build-app-image"), "build")
    restore = _sdr_index(steps, lambda s: s.get("name") == RESTORE_STEP, "restore")
    setup = _sdr_index(steps, lambda s: _calls(s, "setup-deps"), "setup-deps")

    assert build < restore < setup


def test_sdr_e2e_carries_no_workspace_wipe_workarounds() -> None:
    text = SDR_E2E.read_text(encoding="utf-8")

    for leftover in ("/tmp/sdr-e2e", "steps.action_root", "steps.stash"):
        assert leftover not in text, (
            f"{leftover!r} is back in sdr-e2e: setup-deps no longer wipes the "
            "workspace there, so nothing needs a stash"
        )


def _git(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *argv],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_the_restore_reverts_tracked_edits_and_keeps_untracked_paths(
    tmp_path: Path,
) -> None:
    """What the build leaves behind, and what must survive it.

    Tracked edits (the runtime repin of pyproject.toml / uv.lock, the stamped
    Dockerfile) are reverted, so the host side sees the committed tree, as
    when setup-deps re-checked it out. Untracked paths stay: a local action's
    own directory sits in the workspace, and wiping it is what the deleted
    workarounds were for.
    """
    run = next(s for s in _action_steps(SDR_E2E) if s.get("name") == RESTORE_STEP)[
        "run"
    ]
    assert "${{" not in run

    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text("sdk = 'pinned'\n")
    (repo / "uv.lock").write_text("lock = 'pinned'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")

    (repo / "pyproject.toml").write_text("sdk = 'runtime-ref'\n")
    (repo / "uv.lock").write_text("lock = 'runtime-ref'\n")
    (repo / ".application-sdk" / "sdr-e2e").mkdir(parents=True)
    (repo / ".application-sdk" / "sdr-e2e" / "action.yaml").write_text("name: x\n")

    subprocess.run(["bash", "-c", run], cwd=repo, check=True)

    assert (repo / "pyproject.toml").read_text() == "sdk = 'pinned'\n"
    assert (repo / "uv.lock").read_text() == "lock = 'pinned'\n"
    assert (repo / ".application-sdk" / "sdr-e2e" / "action.yaml").is_file()
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
