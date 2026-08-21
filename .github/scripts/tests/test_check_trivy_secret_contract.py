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
_COMPOSITE_PATH = _REPO_ROOT / ".github" / "actions" / "trivy-container" / "action.yaml"

# A caller using `secrets: inherit`, with the fleet App token minted. Values are
# deliberately distinctive so a leak into any output is unmistakable.
FULLY_CONFIGURED = {
    "ORG_PAT_GITHUB": "ghp-SENTINEL-pat-value",
    "APP_TOKEN_MINTED": "ghs-SENTINEL-app-token",
}


class TestSecretsPresent:
    def test_reduces_values_to_booleans(self):
        # The boundary that keeps values out of the reporting path.
        assert guard.secrets_present(FULLY_CONFIGURED) == {
            "ORG_PAT_GITHUB": True,
            "APP_TOKEN_MINTED": True,
        }

    def test_unset_and_empty_are_both_absent(self):
        # GitHub renders an unpassed secret as "", but a caller-side typo in the
        # step's env mapping omits the variable entirely. Both must be caught.
        assert guard.secrets_present({})["ORG_PAT_GITHUB"] is False
        assert guard.secrets_present({"ORG_PAT_GITHUB": ""})["ORG_PAT_GITHUB"] is False

    def test_whitespace_only_is_absent(self):
        assert (
            guard.secrets_present({"ORG_PAT_GITHUB": "  "})["ORG_PAT_GITHUB"] is False
        )

    def test_returns_no_string_values_at_all(self):
        # A regression here would re-open the clear-text-logging path CodeQL
        # flagged: whatever this returns is what reaches failure_message.
        assert all(
            isinstance(v, bool)
            for v in guard.secrets_present(FULLY_CONFIGURED).values()
        )


class TestMissingSecrets:
    def _present(self, **overrides: str) -> dict[str, bool]:
        return guard.secrets_present(FULLY_CONFIGURED | overrides)

    def test_fully_configured_caller_has_nothing_missing(self):
        assert guard.missing_secrets(self._present()) == []

    def test_org_pat_required_when_the_app_token_did_not_mint(self):
        present = self._present(APP_TOKEN_MINTED="", ORG_PAT_GITHUB="")
        assert guard.missing_secrets(present) == ["ORG_PAT_GITHUB"]

    def test_unset_variable_counts_as_missing_not_only_empty(self):
        assert guard.missing_secrets(guard.secrets_present({})) == ["ORG_PAT_GITHUB"]

    def test_org_pat_not_required_when_the_app_token_minted(self):
        # Nothing reads ORG_PAT_GITHUB once the App token exists, so demanding
        # it would red a correctly configured caller.
        assert guard.missing_secrets(self._present(ORG_PAT_GITHUB="")) == []

    def test_whitespace_only_app_token_does_not_count_as_minted(self):
        present = self._present(APP_TOKEN_MINTED="  ", ORG_PAT_GITHUB="")
        assert guard.missing_secrets(present) == ["ORG_PAT_GITHUB"]

    def test_chainguard_secrets_are_no_longer_demanded(self):
        # They exist only as repo-level secrets on application-sdk, so no caller
        # could ever supply them, and no caller builds from cgr.dev anyway.
        # Demanding them is what killed all 33 callers (FND-447).
        assert guard.ALWAYS_REQUIRED == ()
        assert guard.missing_secrets(self._present()) == []

    def test_returns_only_declared_constant_names(self):
        # Every returned element must be a module-level constant, never
        # something derived from the environment.
        known = {*guard.ALWAYS_REQUIRED, guard.REQUIRED_WITHOUT_APP_TOKEN}
        present = self._present(APP_TOKEN_MINTED="", ORG_PAT_GITHUB="")
        assert set(guard.missing_secrets(present)) <= known


class TestFailureMessage:
    def test_names_every_missing_secret(self):
        assert "ORG_PAT_GITHUB" in guard.failure_message(["ORG_PAT_GITHUB"])

    def test_points_the_operator_at_secrets_inherit(self):
        assert "secrets: inherit" in guard.failure_message(["ORG_PAT_GITHUB"])

    def test_points_the_operator_at_the_replacement_workflow(self):
        assert "build-and-scan.yaml" in guard.failure_message(["ORG_PAT_GITHUB"])

    def test_no_secret_value_reaches_the_message(self):
        # End-to-end over the real call chain main() uses, with sentinel values
        # in the environment. This is the assertion CodeQL's finding was about.
        env = FULLY_CONFIGURED | {"APP_TOKEN_MINTED": "", "ORG_PAT_GITHUB": ""}
        message = guard.failure_message(
            guard.missing_secrets(guard.secrets_present(env))
        )
        for value in FULLY_CONFIGURED.values():
            assert value not in message

    def test_printed_text_is_the_constant_not_an_env_derived_string(self):
        # main() must print FAILURE_TEXT, a module-level constant, so CodeQL
        # cannot treat the print as a sink for os.environ. The constant still
        # has to name the only secret this contract can currently fail on.
        assert guard.REQUIRED_WITHOUT_APP_TOKEN in guard.FAILURE_TEXT
        assert "secrets: inherit" in guard.FAILURE_TEXT
        for value in FULLY_CONFIGURED.values():
            assert value not in guard.FAILURE_TEXT


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

    def test_the_chainguard_secrets_are_gone_from_the_contract(self):
        # Re-adding either name resurrects a requirement no caller can satisfy:
        # they are repo-level secrets on application-sdk, invisible elsewhere.
        secrets = self._workflow()[True]["workflow_call"]["secrets"]
        assert "CHAINGUARD_USERNAME" not in secrets
        assert "CHAINGUARD_PASSWORD" not in secrets

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

    def test_the_reusable_does_not_pass_chainguard_to_the_composite(self):
        steps = self._workflow()["jobs"]["build"]["steps"]
        scan = next(s for s in steps if "trivy-container" in s.get("uses", ""))
        assert "chainguard-username" not in scan.get("with", {})
        assert "chainguard-password" not in scan.get("with", {})

    def test_the_header_directs_readers_to_the_replacement(self):
        # This workflow is deprecated; a reader arriving here from a caller must
        # find build-and-scan.yaml without having to dig.
        header = _WORKFLOW_PATH.read_text().split("name:")[0]
        assert "DEPRECATED" in header
        assert "build-and-scan.yaml" in header


# ── The composite must not fail a build that does not need cgr.dev ───────────


class TestCompositeContract:
    def _composite(self) -> dict:
        return yaml.safe_load(_COMPOSITE_PATH.read_text())

    def test_chainguard_inputs_are_optional(self):
        inputs = self._composite()["inputs"]
        assert inputs["chainguard-username"]["required"] is False
        assert inputs["chainguard-password"]["required"] is False

    def test_the_login_step_is_conditional_on_a_username(self):
        # docker/login-action errors on an empty username, so an unconditional
        # login turns "this build does not need cgr.dev" into a hard failure.
        steps = self._composite()["runs"]["steps"]
        login = next(s for s in steps if "Chainguard" in s.get("name", ""))
        assert login.get("if") == "inputs.chainguard-username != ''"
