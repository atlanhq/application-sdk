"""The scoped private-dependency token mint, and every call to it (FND-4901).

A fleet App token that can reach an app's private atlanhq git dependencies is
minted by one composite, ``.github/actions/scoped-private-deps-token``:

1. ``git_auth.py repos`` (the action's own copy, via ``github.action_path``)
   lists the private repos the app's ``pyproject.toml`` / ``uv.lock`` files
   name, refusing any not on ``ALLOWED_PRIVATE_DEPS``;
2. ``actions/create-github-app-token`` mints a read-only token scoped to that
   list, and only when the list is non-empty: no ``repositories`` would grant
   the whole installation. ``continue-on-error``, so
3. a guard decides: unless the caller passes ``require: "false"``, an empty
   token for a non-empty list fails, naming the two secrets.

A caller checks the app out first and calls the action with the fleet App's
secrets. ``require: "false"`` is allowed only at the call sites in
``NOT_REQUIRED``, each of which fails visibly further on without the token.

``conformance-reusable.yaml`` still inlines the same steps (plus a guard):
the SDK's own PRs run that workflow, so it cannot call an action that is not
on ``main`` yet. ``assert_inline_scoped_mint`` pins that copy until it moves.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

Step = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "scoped-private-deps-token"
ACTION = "atlanhq/application-sdk/.github/actions/scoped-private-deps-token@main"
APP_TOKEN_ACTION = "actions/create-github-app-token@"
SCRIPTS_PATH = ".sdk-git-auth"
SECRETS = {
    "app-id": "${{ secrets.FLEET_APP_ID }}",
    "private-key": "${{ secrets.FLEET_APP_PRIVATE_KEY }}",
}

#: (workflow file, job, step id) -> why an empty token is allowed through.
NOT_REQUIRED = {
    ("build-and-publish-app.yaml", "certify-unit", "git-deps-token"): "certify: "
    "the mirror and install fail, and the verdict blocks publish",
    ("build-and-publish-app.yaml", "certify", "git-deps-token"): "certify: "
    "the mirror and install fail, and the verdict blocks publish",
    ("build-and-publish-app.yaml", "build", "image-git-token"): "image "
    "build: minted on every build, only a Dockerfile mounting git_token needs it",
    ("build-and-scan.yaml", "build", "image-git-token"): "image build",
    ("tests-reusable.yaml", "build-e2e-image", "image-git-token"): "image build",
}


def _with(step: Step) -> dict[str, Any]:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _is_checkout(step: Step) -> bool:
    return str(step.get("uses", "")).startswith("actions/checkout@")


def _app_checked_out_before(before: list[Step], project_dir: str) -> None:
    app = [
        s
        for s in before
        if _is_checkout(s)
        and "repository" not in _with(s)
        and _with(s).get("path", ".") == project_dir
    ]
    assert app, f"no checkout of the app at {project_dir!r} before the mint"


def assert_guard_fails(guard: Step) -> None:
    """Run the guard's shell as the runner would (``bash -e``): it must exit
    nonzero and name both secrets. Text that merely mentions ``exit 1`` and
    succeeds would let an unminted token through."""
    bash = shutil.which("bash")
    assert bash, "bash is needed to execute the guard"
    shell = guard.get("shell", "bash")
    assert shell == "bash", f"guard runs under {shell!r}, not bash"
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-eo", "pipefail", "-c", guard["run"]],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0, "the guard exits 0: a failed mint passes"
    output = result.stdout + result.stderr
    assert "::error" in output
    assert "FLEET_APP_ID" in output and "FLEET_APP_PRIVATE_KEY" in output


def calls(steps: list[Step]) -> list[Step]:
    """Every step in *steps* that calls the scoped-mint action."""
    return [s for s in steps if str(s.get("uses", "")).startswith(ACTION[:-5])]


def assert_scoped_mint(
    steps: list[Step],
    *,
    mint_id: str,
    cond: str | None,
    project_dir: str = ".",
    require: str | None = None,
) -> Step:
    """Assert *mint_id* calls the action after checking out the app; return it.

    *require* is the ``require`` input the call must pass (``None``: omitted,
    so the action's default, "true").
    """
    ids = [s.get("id") for s in steps]
    assert ids.count(mint_id) == 1, f"expected one step with id {mint_id!r}"
    at = ids.index(mint_id)
    mint = steps[at]
    assert mint["uses"] == ACTION, mint.get("uses")
    assert mint.get("if") == cond
    # The action's own guard decides; continue-on-error here would hide it.
    assert "continue-on-error" not in mint
    expected = dict(SECRETS)
    if project_dir != ".":
        expected["project-dir"] = project_dir
    if require is not None:
        expected["require"] = require
    assert _with(mint) == expected
    _app_checked_out_before(steps[:at], project_dir)
    return mint


def load_action() -> dict[str, Any]:
    return yaml.safe_load((ACTION_DIR / "action.yaml").read_text(encoding="utf-8"))


def assert_action_shape(spec: dict[str, Any]) -> None:
    """Pin the composite: repos -> scoped, soft mint -> fail-closed guard."""
    assert spec["inputs"]["require"]["default"] == "true"
    assert spec["inputs"]["project-dir"]["default"] == "."
    steps = spec["runs"]["steps"]
    assert [s.get("id") for s in steps] == ["git-deps-repos", "git-deps-token", None]
    repos, mint, guard = steps

    # The action's own copy of the script: never the app's tree, never written
    # into the workspace (the image build context).
    assert repos["run"] == (
        'python3 "${{ github.action_path }}/../../scripts/git_auth.py" repos '
        '--project-dir "$PROJECT_DIR"'
    )
    assert repos["env"] == {"PROJECT_DIR": "${{ inputs.project-dir }}"}
    assert "if" not in repos

    assert str(mint["uses"]).startswith(APP_TOKEN_ACTION)
    assert mint["if"] == "steps.git-deps-repos.outputs.repositories != ''"
    assert mint.get("continue-on-error") is True
    assert _with(mint) == {
        "app-id": "${{ inputs.app-id }}",
        "private-key": "${{ inputs.private-key }}",
        "owner": "atlanhq",
        "repositories": "${{ steps.git-deps-repos.outputs.repositories }}",
        "permission-contents": "read",
    }

    # Fail closed: anything but an explicit "false" requires the token.
    assert guard["if"] == (
        "inputs.require != 'false' && "
        "steps.git-deps-repos.outputs.repositories != '' && "
        "steps.git-deps-token.outputs.token == ''"
    )
    assert_guard_fails(guard)

    outputs = spec["outputs"]
    assert outputs["token"]["value"] == "${{ steps.git-deps-token.outputs.token }}"


def assert_inline_scoped_mint(
    steps: list[Step],
    *,
    mint_id: str,
    repos_id: str,
    cond: str,
    project_dir: str = ".",
) -> Step:
    """The inline copy: checkout script -> repos -> soft mint -> guard."""
    ids = [s.get("id") for s in steps]
    assert ids.count(mint_id) == 1, f"expected one step with id {mint_id!r}"
    assert ids.count(repos_id) == 1, f"expected one step with id {repos_id!r}"
    mint_at = ids.index(mint_id)
    repos_at = ids.index(repos_id)
    assert repos_at < mint_at

    repos = steps[repos_at]
    assert repos.get("if") == cond
    run = repos["run"]
    assert (
        f"python3 {SCRIPTS_PATH}/.github/scripts/git_auth.py repos "
        f"--project-dir {project_dir}"
    ) in run
    assert f"rm -rf {SCRIPTS_PATH}" in run
    assert run.index("git_auth.py repos") < run.index(f"rm -rf {SCRIPTS_PATH}")

    before = steps[:repos_at]
    scripts = [
        s for s in before if _is_checkout(s) and _with(s).get("path") == SCRIPTS_PATH
    ]
    assert scripts, "git_auth.py is not checked out before the repos step"
    w = _with(scripts[-1])
    assert w["repository"] == "atlanhq/application-sdk"
    assert w["ref"] == "${{ job.workflow_sha }}"
    assert w["sparse-checkout"] == ".github/scripts/git_auth.py"
    assert w["persist-credentials"] is False
    assert scripts[-1].get("if") == cond
    _app_checked_out_before(before, project_dir)

    mint = steps[mint_at]
    repos_ref = f"steps.{repos_id}.outputs.repositories"
    assert str(mint["uses"]).startswith(APP_TOKEN_ACTION)
    assert mint["if"] == f"{cond} && {repos_ref} != ''"
    assert mint.get("continue-on-error") is True
    w = _with(mint)
    assert w["repositories"] == f"${{{{ {repos_ref} }}}}"
    assert w["permission-contents"] == "read"
    assert w["owner"] == "atlanhq"
    assert w["app-id"] == SECRETS["app-id"]
    assert w["private-key"] == SECRETS["private-key"]
    assert set(w) == {*SECRETS, "owner", "repositories", "permission-contents"}

    # The soft mint is only safe with the guard straight after it.
    guard = steps[mint_at + 1]
    assert guard.get("if") == (
        f"{cond} && {repos_ref} != '' && steps.{mint_id}.outputs.token == ''"
    ), "the inline mint is not followed by its fail-closed guard"
    assert_guard_fails(guard)
    return mint
