"""Tests for .github/scripts/check_trivy_secret_contract.py."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_trivy_secret_contract as guard

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / ".github" / "scripts" / "check_trivy_secret_contract.py"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "trivy-container.yaml"

# A caller using `secrets: inherit`, with the fleet App token minted.
FULLY_CONFIGURED = {
    "CHAINGUARD_USERNAME": "user",
    "CHAINGUARD_PASSWORD": "pass",
    "ORG_PAT_GITHUB": "pat",
    "APP_TOKEN_MINTED": "ghs_token",
}

# The FND-447 shape: an explicit `secrets:` block passing only ORG_PAT_GITHUB.
EXPLICIT_BLOCK_ONLY_PAT = {
    "CHAINGUARD_USERNAME": "",
    "CHAINGUARD_PASSWORD": "",
    "ORG_PAT_GITHUB": "pat",
    "APP_TOKEN_MINTED": "",
}


class TestMissingSecrets:
    def test_fully_configured_caller_has_nothing_missing(self):
        assert guard.missing_secrets(FULLY_CONFIGURED) == []

    def test_reports_both_chainguard_secrets_for_the_fnd447_shape(self):
        assert guard.missing_secrets(EXPLICIT_BLOCK_ONLY_PAT) == [
            "CHAINGUARD_USERNAME",
            "CHAINGUARD_PASSWORD",
        ]

    def test_unset_variable_counts_as_missing_not_only_empty(self):
        # GitHub renders an unpassed secret as "", but a caller-side typo in the
        # step's env mapping omits the variable entirely. Both must be caught.
        assert guard.missing_secrets({"APP_TOKEN_MINTED": "ghs_token"}) == [
            "CHAINGUARD_USERNAME",
            "CHAINGUARD_PASSWORD",
        ]

    def test_org_pat_required_when_the_app_token_did_not_mint(self):
        env = FULLY_CONFIGURED | {"APP_TOKEN_MINTED": "", "ORG_PAT_GITHUB": ""}
        assert guard.missing_secrets(env) == ["ORG_PAT_GITHUB"]

    def test_org_pat_not_required_when_the_app_token_minted(self):
        # Nothing reads ORG_PAT_GITHUB once the App token exists, so demanding
        # it would red a correctly configured caller.
        assert guard.missing_secrets(FULLY_CONFIGURED | {"ORG_PAT_GITHUB": ""}) == []

    def test_whitespace_only_app_token_does_not_count_as_minted(self):
        env = FULLY_CONFIGURED | {"APP_TOKEN_MINTED": "  ", "ORG_PAT_GITHUB": ""}
        assert guard.missing_secrets(env) == ["ORG_PAT_GITHUB"]


class TestFailureMessage:
    def test_names_every_missing_secret(self):
        message = guard.failure_message(["CHAINGUARD_USERNAME", "ORG_PAT_GITHUB"])
        assert "CHAINGUARD_USERNAME" in message
        assert "ORG_PAT_GITHUB" in message

    def test_points_the_operator_at_secrets_inherit(self):
        assert "secrets: inherit" in guard.failure_message(["CHAINGUARD_PASSWORD"])


# ── The reusable must stay incapable of startup_failure ──────────────────────
# The guarantee lives in the workflow YAML, not in the script, so this is where
# it gets regression-tested. Re-adding `required: true` to any secret is a
# one-word edit that looks like a tightening and is in fact the opposite: it
# moves the failure from a red step back to `startup_failure`, which emits no
# check run and so cannot be seen or gated on (FND-447).


class TestWorkflowContract:
    def _workflow(self) -> dict:
        # PyYAML resolves the bare `on:` key to the boolean True.
        return yaml.safe_load(_WORKFLOW_PATH.read_text())

    def test_no_secret_is_declared_required(self):
        secrets = self._workflow()[True]["workflow_call"]["secrets"]
        required = [
            name for name, spec in secrets.items() if (spec or {}).get("required")
        ]
        assert required == [], (
            f"trivy-container.yaml declares {required} as `required: true`. A "
            "caller omitting one then ends in startup_failure with zero jobs "
            "and no check run. Declare it `required: false` and let "
            "check_trivy_secret_contract.py enforce it inside the job instead."
        )

    def test_every_enforced_secret_is_declared_by_the_workflow(self):
        # A secret the script demands but the workflow never declares can never
        # be passed, so the step would red unconditionally.
        declared = set(self._workflow()[True]["workflow_call"]["secrets"])
        enforced = set(guard.ALWAYS_REQUIRED) | {guard.REQUIRED_WITHOUT_APP_TOKEN}
        assert (
            enforced <= declared
        ), f"not declared by the workflow: {enforced - declared}"

    def test_workflow_invokes_this_script_at_a_real_path(self):
        steps = self._workflow()["jobs"]["build"]["steps"]
        invocations = [
            s["run"]
            for s in steps
            if "check_trivy_secret_contract.py" in s.get("run", "")
        ]
        assert len(invocations) == 1, (
            "trivy-container.yaml no longer invokes check_trivy_secret_contract.py "
            "exactly once — without it every secret is unenforced."
        )
        # The reusable runs in the caller's workspace, where this repo is
        # sparse-checked out at _sdk/, so the invoked path is _sdk-prefixed.
        assert "_sdk/.github/scripts/check_trivy_secret_contract.py" in invocations[0]
        assert _MODULE_PATH.is_file()

    def test_the_verify_step_maps_every_enforced_secret_into_its_env(self):
        steps = self._workflow()["jobs"]["build"]["steps"]
        verify = next(
            s for s in steps if "check_trivy_secret_contract.py" in s.get("run", "")
        )
        # An enforced secret absent from `env:` reads as empty no matter what
        # the caller passed, so the step would red on a correct configuration.
        for name in (*guard.ALWAYS_REQUIRED, guard.REQUIRED_WITHOUT_APP_TOKEN):
            assert name in verify["env"], f"verify step does not map {name} into env"

    def test_the_verify_step_runs_before_the_scan(self):
        steps = self._workflow()["jobs"]["build"]["steps"]
        verify_at = next(
            i
            for i, s in enumerate(steps)
            if "check_trivy_secret_contract.py" in s.get("run", "")
        )
        scan_at = next(
            i for i, s in enumerate(steps) if "trivy-container" in s.get("uses", "")
        )
        assert verify_at < scan_at, (
            "the secret check must precede the scan composite, or the operator "
            "sees docker/login-action's cryptic error instead of the remedy."
        )
