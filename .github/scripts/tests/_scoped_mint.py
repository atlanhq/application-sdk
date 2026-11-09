"""The scoped private-dependency token mint, as every workflow inlines it.

A fleet App token that can reach an app's private atlanhq git dependencies is
minted in four steps, in this order (lens round 5 on #4731):

1. the app is checked out (its ``pyproject.toml`` / ``uv.lock`` files name the
   dependencies);
2. ``.github/scripts/git_auth.py`` is sparse-checked-out from this workflow's
   own commit (``job.workflow_sha``) at ``.sdk-git-auth``;
3. ``git_auth.py repos`` lists the private repos the app names, then removes
   ``.sdk-git-auth`` from the workspace (the image build context, and the tree
   conformance and pre-commit look at);
4. ``actions/create-github-app-token`` mints a read-only token scoped to that
   list, and only when the list is non-empty: no ``repositories`` would grant
   the whole installation.

Inline, not a composite action: callers pin composites ``@main``, so a new one
cannot run before it merges, and composite steps cannot ``continue-on-error``.
"""

from __future__ import annotations

from typing import Any

Step = dict[str, Any]

APP_TOKEN_ACTION = "actions/create-github-app-token@"
SCRIPTS_PATH = ".sdk-git-auth"


def _with(step: Step) -> dict[str, Any]:
    value = step.get("with")
    return value if isinstance(value, dict) else {}


def _is_checkout(step: Step) -> bool:
    return str(step.get("uses", "")).startswith("actions/checkout@")


def assert_scoped_mint(
    steps: list[Step],
    *,
    mint_id: str,
    repos_id: str,
    cond: str | None,
    project_dir: str = ".",
) -> Step:
    """Assert the four-step shape for the mint *mint_id*; return the mint step."""
    ids = [s.get("id") for s in steps]
    assert ids.count(mint_id) == 1, f"expected one step with id {mint_id!r}"
    assert ids.count(repos_id) == 1, f"expected one step with id {repos_id!r}"
    mint_at = ids.index(mint_id)
    repos_at = ids.index(repos_id)
    assert repos_at < mint_at

    gate = f"{cond} && " if cond else ""
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
    # The workflow's own commit, never a caller-chosen or default-branch ref.
    assert w["ref"] == "${{ job.workflow_sha }}"
    assert w["sparse-checkout"] == ".github/scripts/git_auth.py"
    assert w["persist-credentials"] is False
    assert scripts[-1].get("if") == cond

    app = [
        s
        for s in before
        if _is_checkout(s)
        and "repository" not in _with(s)
        and _with(s).get("path", ".") == project_dir
    ]
    assert app, f"no checkout of the app at {project_dir!r} before the repos step"

    mint = steps[mint_at]
    assert str(mint["uses"]).startswith(APP_TOKEN_ACTION)
    assert mint["if"] == f"{gate}steps.{repos_id}.outputs.repositories != ''"
    # A failed mint leaves the token empty for the caller's named guard (or,
    # for an image build, an empty secret only a build that needs it trips on).
    assert mint.get("continue-on-error") is True
    w = _with(mint)
    assert w["repositories"] == f"${{{{ steps.{repos_id}.outputs.repositories }}}}"
    assert w["permission-contents"] == "read"
    assert w["owner"] == "atlanhq"
    assert "FLEET_APP_ID" in w["app-id"]
    assert "FLEET_APP_PRIVATE_KEY" in w["private-key"]
    assert set(w) == {
        "app-id",
        "private-key",
        "owner",
        "repositories",
        "permission-contents",
    }
    return mint
