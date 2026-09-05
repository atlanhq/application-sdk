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

import os
import subprocess
import sys
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

    The shell reads ``application_sdk.app._generated_tree`` — the same authority
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
            "application_sdk.app._generated_tree, which the configmap endpoint "
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


# ── Version skew: the action is @main, the SDK is per-app-pinned ─────────────
#
# Both callers reference this action as `@main`, so a change here reaches every
# repo's next e2e run at once. The SDK it imports is the CONNECTOR's own pinned
# version — the harness is repinned only on cross-repo dispatch
# (`harness-sdk-ref`), and the action's own comment says an ordinary connector
# PR runs "the connector's OWN pinned SDK".
#
# So without a guard, merging this reds the e2e leg of every connector still
# pinned below the release that adds the check — a fleet-wide break caused
# purely by skew, with nothing wrong in any app. These tests are the guard's
# regression net, and the subprocess one is the only one that proves the
# property rather than describing it.


def _run_shell(
    env_extra: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    """Run the CLI shell in a subprocess with a modified environment."""
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_SHELL), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _fake_older_sdk(root: Path) -> Path:
    """An `application_sdk` package whose `testing` has no `setup_routes`.

    This is what an older pinned SDK looks like to the probe: the package and
    the subpackage import fine, and the submodule simply is not there.
    """
    package = root / "application_sdk"
    (package / "testing").mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "3.0.0"\n')
    (package / "testing" / "__init__.py").write_text("")
    return root


def test_an_older_pinned_sdk_skips_instead_of_crashing(tmp_path: Path) -> None:
    """The fleet-safety property, proven by running the real shell.

    A `ModuleNotFoundError` here would be a red e2e leg in every connector
    pinned below the release that adds the check.
    """
    result = _run_shell(
        {"PYTHONPATH": str(_fake_older_sdk(tmp_path)), "ATLAN_API_KEY": "unused"},
        "--base-url",
        "https://tenant.invalid",
    )

    assert result.returncode == 0, (
        "the shell did not survive an SDK that predates the check:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ModuleNotFoundError" not in result.stderr
    assert "::notice::" in result.stdout


def test_the_skip_notice_says_no_action_is_needed_in_the_app(tmp_path: Path) -> None:
    """A skew skip must not read as a task for the connector's author.

    The check ships with the SDK, so the skip clears itself when the pin moves.
    Saying so is what stops someone opening an issue against their own repo.
    """
    result = _run_shell(
        {"PYTHONPATH": str(_fake_older_sdk(tmp_path)), "ATLAN_API_KEY": "unused"},
        "--base-url",
        "https://tenant.invalid",
    )

    assert "predates" in result.stdout
    assert "nothing needs" in result.stdout


def test_the_skip_does_not_require_a_tenant_token(tmp_path: Path) -> None:
    """The skew skip comes before the credential read.

    An old-pin repo has no reason to have supplied one, and demanding it would
    turn the skip back into the failure the guard exists to prevent.
    """
    env = {"PYTHONPATH": str(_fake_older_sdk(tmp_path))}
    # Blank rather than absent: the resolver exports these, so an empty value is
    # the realistic shape, and `_bearer` treats both the same way.
    env["ATLAN_API_KEY"] = ""
    env["E2E_API_KEY"] = ""

    result = _run_shell(env, "--base-url", "https://tenant.invalid")

    assert result.returncode == 0
    assert "no tenant token" not in result.stdout + result.stderr


def test_a_current_sdk_does_not_take_the_skip_path() -> None:
    """The guard must not swallow the check on an SDK that DOES carry it.

    A probe that answered "absent" for a present module would green every leg
    forever — the worst outcome available here, since it looks identical to a
    pass. Run with no PYTHONPATH shim, i.e. this repo's own SDK.
    """
    result = _run_shell({"ATLAN_API_KEY": ""}, "--base-url", "https://tenant.invalid")

    assert "predates" not in result.stdout, (
        "the shell reported an SDK that predates the check while running "
        "against this repo's own SDK, which contains it"
    )
    # It gets as far as needing a credential, which proves it took the real path.
    assert result.returncode == 1
    assert "no tenant token" in result.stderr


def test_the_probe_is_an_import_check_not_a_version_comparison() -> None:
    """No release-number floor to keep in step with a changelog.

    A version gate needs a constant bumped by hand at release time; get it
    wrong in either direction and the check either crashes on old pins or
    silently skips new ones. The import probe asks the question that actually
    matters and closes on its own as apps bump.
    """
    source = _SHELL.read_text(encoding="utf-8")

    assert "importlib.util.find_spec" in source
    for version_gate in ("__version__", "packaging", "Version("):
        assert version_gate not in source, (
            f"{version_gate!r} suggests a version comparison; the guard is "
            "deliberately an import probe (see the module docstring)"
        )
