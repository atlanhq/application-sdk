"""Who the shared Renovate approval posts as — the token/login wiring in
.github/workflows/renovate-auto-approve-reusable.yml.

The reusable serves two kinds of caller with different approval needs:

  * connector repos, whose rulesets require no code-owner review, approve as the
    dedicated approver App;
  * application-sdk, whose ruleset does require one, approves as the SDK
    approver User (Apps cannot be CODEOWNERS), so the App step must never run
    there.

Everything else falls back to the atlan-ci PAT. The token and APPROVER_LOGIN are
chosen by GHA expressions, so the real expressions are lifted out of the YAML
and evaluated per scenario. The one invariant that matters most: the login the
gate is told must be the identity GH_TOKEN actually acts as, or condition (g)
misses the gate's own approvals and posts duplicates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate, evaluate_operand  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/renovate-auto-approve-reusable.yml"

APP_TOKEN = "ghs_app"
SDK_PAT = "github_pat_sdk"
ORG_PAT = "ghp_org"


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    return workflow["jobs"]["renovate-auto-approve"]["steps"]


def _step(step_id: str | None = None, *, runs: str | None = None) -> dict[str, Any]:
    for step in _steps():
        if step_id is not None and step.get("id") == step_id:
            return step
        if runs is not None and runs in str(step.get("run", "")):
            return step
    raise AssertionError(f"step not found (id={step_id!r}, runs={runs!r})")


def _gate_env() -> dict[str, str]:
    return _step(runs="renovate_approval_conditions.py")["env"]


def _contexts(
    *,
    repo: str,
    app_id: str = "",
    app_token: str = "",
    app_slug: str = "",
    secrets: dict[str, str] | None = None,
    sdk_login: str = "",
) -> dict[str, Any]:
    owner, name = repo.split("/")
    return {
        "github": {
            "repository": repo,
            "repository_owner": owner,
            "event": {"repository": {"name": name}},
        },
        "vars": {"PR_APPROVER_APP_ID": app_id, "SDK_APPROVER_LOGIN": sdk_login},
        "secrets": secrets or {},
        "steps": {
            "approver-app": {"outputs": {"token": app_token, "app-slug": app_slug}}
        },
    }


def _resolve(ctx: dict[str, Any]) -> tuple[Any, Any]:
    env = _gate_env()
    return (
        evaluate_operand(env["GH_TOKEN"], ctx),
        evaluate_operand(env["APPROVER_LOGIN"], ctx),
    )


class TestAppStepGate:
    def _runs(self, **kwargs) -> bool:
        return evaluate(_step("approver-app")["if"], _contexts(**kwargs))

    def test_runs_on_a_connector_repo_with_the_app_configured(self):
        assert self._runs(repo="atlanhq/atlan-mysql-app", app_id="123")

    def test_skipped_when_the_app_is_not_configured(self):
        assert not self._runs(repo="atlanhq/atlan-mysql-app", app_id="")

    def test_never_runs_on_application_sdk(self):
        # Its ruleset requires a code owner; an App review cannot satisfy it,
        # so even a mis-scoped org variable must not route the SDK to the App.
        assert not self._runs(repo="atlanhq/application-sdk", app_id="123")

    def test_failure_degrades_instead_of_failing_the_job(self):
        assert _step("approver-app")["continue-on-error"] is True

    def test_token_is_scoped_to_the_calling_repo_and_minimal_permissions(self):
        with_ = _step("approver-app")["with"]
        assert with_["repositories"] == "${{ github.event.repository.name }}"
        perms = {k: v for k, v in with_.items() if k.startswith("permission-")}
        assert perms == {
            "permission-pull-requests": "write",
            "permission-contents": "read",
            "permission-checks": "read",
            "permission-statuses": "read",
        }


class TestTokenAndLoginAgree:
    @pytest.mark.parametrize(
        ("name", "ctx", "token", "login"),
        [
            (
                "connector, App minted",
                _contexts(
                    repo="atlanhq/atlan-mysql-app",
                    app_id="123",
                    app_token=APP_TOKEN,
                    app_slug="atlan-pr-approver",
                    secrets={"ORG_PAT_GITHUB": ORG_PAT},
                ),
                APP_TOKEN,
                "atlan-pr-approver[bot]",
            ),
            (
                "connector, App not installed (mint failed)",
                _contexts(
                    repo="atlanhq/atlan-mysql-app",
                    app_id="123",
                    secrets={"ORG_PAT_GITHUB": ORG_PAT},
                ),
                ORG_PAT,
                "atlan-ci",
            ),
            (
                "connector, legacy explicit org_pat caller",
                _contexts(repo="atlanhq/atlan-mysql-app", secrets={"org_pat": ORG_PAT}),
                ORG_PAT,
                "atlan-ci",
            ),
            (
                "application-sdk with the SDK approver provisioned",
                _contexts(
                    repo="atlanhq/application-sdk",
                    secrets={"SDK_APPROVER_TOKEN": SDK_PAT, "ORG_PAT_GITHUB": ORG_PAT},
                    sdk_login="atlan-sdk-approver",
                ),
                SDK_PAT,
                "atlan-sdk-approver",
            ),
            (
                "application-sdk, SDK PAT on the atlan-ci account (no login var)",
                _contexts(
                    repo="atlanhq/application-sdk",
                    secrets={"SDK_APPROVER_TOKEN": SDK_PAT, "ORG_PAT_GITHUB": ORG_PAT},
                ),
                SDK_PAT,
                "atlan-ci",
            ),
            (
                "application-sdk, login var set but PAT not yet stored",
                # The login must follow the token that won, not the variable.
                _contexts(
                    repo="atlanhq/application-sdk",
                    secrets={"ORG_PAT_GITHUB": ORG_PAT},
                    sdk_login="atlan-sdk-approver",
                ),
                ORG_PAT,
                "atlan-ci",
            ),
        ],
    )
    def test_login_names_the_identity_the_token_acts_as(self, name, ctx, token, login):
        assert _resolve(ctx) == (token, login), name
