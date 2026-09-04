"""Guards for the workflow-setup route check's wiring in sdr-e2e (FND-1667).

The check itself is unit-tested in ``tests/unit/testing/test_setup_routes.py``,
which proves it bites. These assertions protect the *placement*, and every one
of them guards a failure mode that is silent rather than red:

* placed in the wrong job, the SDK is not importable and the step dies on an
  ``ImportError`` that reads like a broken check rather than a misplaced one
* placed before the install, it reports a 404 for an app that simply is not
  deployed yet — a false positive on every run
* placed after the assertions, a connector whose setup page will not load still
  runs a full DAG against it first
* gated wrongly, it compares a committed contract against whatever version the
  tenant already happened to serve, and reports a stale image as a contract
  break

Deliberately YAML-shape assertions: what is being protected is GitHub Actions'
own step ordering and job-dependency semantics, which cannot be exercised
without a runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/tests-reusable.yaml"
_FULL_WORKFLOW = _REPO_ROOT / ".github/workflows/e2e-full-reusable.yaml"
_SDR_ACTION = _REPO_ROOT / ".github/actions/sdr-e2e/action.yaml"
_SHELL = _REPO_ROOT / ".github/actions/sdr-e2e/verify_setup_routes.py"

_STEP_NAME = "Verify workflow-setup routes resolve"
_VERSION_STEP_NAME = "Verify the tenant runs the version under test"
_PYTEST_STEP_NAME = "Run SDR integration tests"


@pytest.fixture(scope="module")
def action() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(_SDR_ACTION.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def steps(action: dict) -> list[dict]:  # type: ignore[type-arg]
    return action["runs"]["steps"]


def _index(steps: list[dict], name: str) -> int:  # type: ignore[type-arg]
    for position, step in enumerate(steps):
        if step.get("name") == name:
            return position
    raise AssertionError(f"no step named {name!r} in {_SDR_ACTION}")


# ── It exists, and it runs the shell that exists ─────────────────────────────


def test_the_step_exists(steps: list[dict]) -> None:  # type: ignore[type-arg]
    _index(steps, _STEP_NAME)


def test_the_shell_it_invokes_is_committed(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """The step's script must actually be in the action directory.

    ``$SDR_E2E_ROOT`` resolves to a copy of this directory, so a step naming a
    file that is not committed here fails at runtime with a bare "No such file",
    several minutes into an e2e leg.
    """
    step = steps[_index(steps, _STEP_NAME)]

    assert "verify_setup_routes.py" in step["run"]
    assert _SHELL.is_file(), f"{_SHELL} is referenced by the step but not committed"


def test_it_runs_under_uv_not_bare_python(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """`uv run python`, because the check imports the SDK.

    The shell reads ``application_sdk.app.generated_tree`` — the same authority
    the configmap endpoint serves from — rather than keeping a second copy of
    the layout and form-selection rules. A bare ``python3`` has no SDK on the
    path, so this would die on an ImportError. The action's other scripts use
    ``python3`` precisely because they do NOT need the SDK, which makes this an
    easy thing to "tidy" into breakage.
    """
    step = steps[_index(steps, _STEP_NAME)]

    assert "uv run python" in step["run"]
    assert "python3 " not in step["run"]


# ── Ordering: after the install, before the assertions ───────────────────────


def test_it_runs_after_the_version_verify(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """The version check is the cheaper, more fundamental failure.

    A tenant running the wrong version would make the served-form subset check
    report a stale image as a contract break, so the version mismatch has to be
    the error the reader sees first.
    """
    assert _index(steps, _STEP_NAME) > _index(steps, _VERSION_STEP_NAME)


def test_it_runs_before_the_assertions(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """An app whose setup page will not load is not usefully installed.

    There is nothing to learn from running a full DAG against it, so this fails
    the leg before the suite spends its budget.
    """
    assert _index(steps, _STEP_NAME) < _index(steps, _PYTEST_STEP_NAME)


def test_the_calling_job_runs_after_prepare_tenant() -> None:
    """The cards and configmaps exist only after the install.

    This is what makes placing the step in this composite sound at all: the
    ``e2e`` job that invokes it declares ``prepare-tenant`` in its ``needs:``,
    so every step here is strictly after the install. Deleting that dependency
    would leave this check reading a tenant nobody installed onto.
    """
    jobs = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))["jobs"]

    assert "prepare-tenant" in jobs["e2e"]["needs"]


# ── The gate ─────────────────────────────────────────────────────────────────


def test_it_shares_the_install_paths_gate(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """Gated on `expected-app-version`, exactly like the version verify.

    That input is non-empty precisely when THIS run installed the app under
    test. A caller that did not install is pointed at whatever the tenant
    already served, and the served-form subset check would then read a stale
    image as a missing contract input — a false positive, on a check whose
    entire value is that it does not produce those.
    """
    step = steps[_index(steps, _STEP_NAME)]
    version_step = steps[_index(steps, _VERSION_STEP_NAME)]

    assert step["if"] == version_step["if"]
    assert "expected-app-version" in step["if"]


# ── Injection discipline ─────────────────────────────────────────────────────


def test_nothing_is_interpolated_into_the_run_block(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """Everything through `env:`, nothing through `${{ }}` inside `run:`.

    docs/standards/ci.md, and the same finding sdk-review has raised against
    this file's neighbours: ``github.head_ref`` is attacker-controlled on a fork
    PR, and a crafted branch name interpolated into ``run:`` is spliced into the
    script before bash sees a quote.
    """
    step = steps[_index(steps, _STEP_NAME)]

    assert "${{" not in step["run"]


def test_the_token_never_reaches_argv(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """The credential is read from the environment inside the shell.

    argv is visible in process listings and in ``set -x`` output, so a token
    passed as a flag leaks to anything that can read either.
    """
    step = steps[_index(steps, _STEP_NAME)]

    assert "ATLAN_API_KEY" not in step["run"]
    assert "--token" not in step["run"]
    assert "--api-key" not in step["run"]


# ── Both callers get it ──────────────────────────────────────────────────────


@pytest.mark.parametrize("workflow", [_WORKFLOW, _FULL_WORKFLOW])
def test_both_callers_reach_this_action(workflow: Path) -> None:
    """One insertion has to cover every e2e caller, or it covers one repo.

    The check lives in this composite rather than in a job step precisely
    because both reusable e2e workflows call it. If a caller stopped using the
    action, its route coverage would vanish with no failing check to say so.
    """
    assert "actions/sdr-e2e" in workflow.read_text(encoding="utf-8")


# ── No second copy of the shared rules ───────────────────────────────────────


def test_the_shell_holds_no_check_logic() -> None:
    """The shell parses arguments and prints; the logic is in the SDK.

    The point of the placement is that there is ONE copy of "which generated
    file is the form", "what layout is this tree" and "how is the served
    envelope shaped" — read from the same module the configmap endpoint serves
    from. A copy re-appearing here would let the server serve one file while the
    check compared against another, and that mismatch would read as a contract
    regression rather than as two divergent copies of one rule.
    """
    source = _SHELL.read_text(encoding="utf-8")

    assert "from application_sdk.testing.setup_routes import" in source
    # The vocabulary that must NOT be restated here.
    for leaked in ("atlan-connectors-", "csa-connectors-", "manifest.json"):
        assert leaked not in source, (
            f"{leaked!r} appears in the CLI shell. That rule belongs to "
            "application_sdk.app.generated_tree, which the configmap endpoint "
            "also reads — a second copy here can drift from the server."
        )


def test_the_action_stash_copies_the_whole_directory(steps: list[dict]) -> None:  # type: ignore[type-arg]
    """The shell reaches the runner via the action's own directory stash.

    When this action is invoked as a LOCAL action, ``github.action_path`` sits
    inside the workspace and ``setup-deps``' checkout wipes it, so every
    post-setup step reads from a ``/tmp`` stash taken beforehand. That stash is
    a whole-directory ``cp -R``, which is why adding a script needed no change
    to it. Narrowing it to an explicit file list would silently strip this one
    — the step would then fail minutes into an e2e leg with a bare "No such
    file or directory".
    """
    stash = steps[_index(steps, "Stash action assets")]

    assert "cp -R" in stash["run"]
    assert "verify_setup_routes" not in stash["run"], (
        "the stash names individual files now; add verify_setup_routes.py to it "
        "or restore the whole-directory copy"
    )
