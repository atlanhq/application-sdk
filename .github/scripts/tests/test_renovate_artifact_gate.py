"""Behavioural guard for the renovate/artifacts approval condition (FND-362).

The Renovate auto-approval withholds the atlan-ci code-owner approval unless
Renovate's own `renovate/artifacts` commit status is green. That gate is the only
thing stopping a branch whose artifacts failed to update from auto-merging:
Renovate records the error, then commits and raises the PR regardless, and
platform automerge never consults `artifactErrors` — so the PR merges on the
strength of the checks it did pass, with nothing red to show for the part that
did not happen. The contract-toolkit lane is the live case: if the
renovate-pkl-sync driver fails or is skipped, the PR lands a toolkit version bump
whose regenerated `app/generated/**` does not match it.

Note the failure this has to survive is a silent one. A post-upgrade command the
runner's admin-only `allowedCommands` allowlist does not match is skipped with a
log line and nothing else, so "the command did not run" and "the command ran
clean" are indistinguishable without this status.

(The gate was built for a 7-day release-age bound on the uv.lock lane, which was
removed fleet-wide in FND-367 — the reasoning above never depended on it. Do not
retire this alongside anything cooldown-shaped; see FND-359 if it returns.)

**Scope of this file.** Classification and gate wiring are now ordinary unit
tests over `renovate_approval_conditions.py`, and live in
`test_renovate_approval_conditions.py` alongside the other six conditions — this
file no longer lifts a `gh api --jq` line out of the workflow YAML and executes
it against a stubbed shell (FND-372 moved the logic out of the YAML, so that
scaffolding had nothing left to test).

What remains here are the two properties that are NOT expressible inside the
gate, because they are about the world the gate assumes:

  1. the fleet Renovate preset publishes the status on healthy branches, which
     is the only reason "missing" may be read as not-green rather than stalling
     every clean PR; and
  2. the workflow still routes through the driver that enforces it at all.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import renovate_approval_conditions as gate  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/renovate-auto-approve-reusable.yml"
_DRIVER = "renovate_approval_conditions.py"


def _approval_step_run() -> str:
    """The shell body of the step that runs the approval gate."""
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    steps = workflow["jobs"]["renovate-auto-approve"]["steps"]
    for step in steps:
        run = step.get("run", "")
        if _DRIVER in run:
            return run
    raise AssertionError(f"no step in {_WORKFLOW.name} invokes {_DRIVER}")


class TestPresetContract:
    def test_preset_publishes_the_status_on_healthy_branches(self):
        # The gate treats a missing context as not-green, which is only safe
        # because the fleet preset flips statusCheckWhen.artifactError to
        # "always". Renovate's default ("failed") publishes the context only on
        # error, and every healthy branch would stall unapproved.
        preset = json.loads((_REPO_ROOT / "renovate-config/default.json").read_text())
        assert preset["statusCheckWhen"]["artifactError"] == "always"

    def test_gate_reads_the_context_the_preset_publishes(self):
        # Both halves of the contract have to name the same string; a rename on
        # either side silently turns every PR's status into "missing".
        assert gate.ARTIFACT_CONTEXT == "renovate/artifacts"


class TestWorkflowWiring:
    def test_workflow_invokes_the_gate_driver(self):
        # Presence of the condition in Python is worth nothing if the workflow
        # stops calling the script that evaluates it.
        assert _DRIVER in _approval_step_run()

    def test_approval_step_has_no_inlined_conditional_shell(self):
        # docs/standards/ci.md: the `run:` block must be a straight-line
        # invocation. This is also what keeps the conditions testable at all —
        # the reason this file no longer has to lift a line out of the YAML.
        run = _approval_step_run()
        for keyword in ("if ", "else", "fi", "for ", "while ", "case ", "continue"):
            assert (
                keyword not in run
            ), f"conditional shell ({keyword!r}) is back in the run: block"

    def test_script_checkout_ref_is_a_literal_that_cannot_be_empty(self):
        # Regression, found by piloting this gate from a consumer repo before
        # merge: `ref: ${{ github.job_workflow_sha }}` renders EMPTY, so
        # actions/checkout treated the input as unsupplied and fetched the
        # default branch — a GREEN step that silently checked out a different
        # commit's script than the workflow the caller pinned. It only surfaced
        # because the script did not exist on main yet; after merge that mode
        # would have run the wrong version quietly.
        #
        # `main` is the right literal because every caller pins the reusable at
        # @main. The assertion that matters is the second one: the ref must not
        # come from an expression at all, since any expression can evaluate to
        # empty and checkout reads empty as "not supplied".
        workflow = yaml.safe_load(_WORKFLOW.read_text())
        steps = workflow["jobs"]["renovate-auto-approve"]["steps"]
        checkout = next(
            s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")
        )
        ref = checkout["with"]["ref"]
        assert ref == "main"
        assert "${{" not in str(ref), (
            "the checkout ref must be a literal — an expression that evaluates "
            "to empty makes checkout silently take the default branch"
        )
        assert checkout["with"]["path"] == ".sdk-scripts"

    def test_driver_path_resolves(self):
        # The reusable sparse-checks-out the SDK scripts into .sdk-scripts, so
        # the invoked path is prefixed. Assert the file it points at exists.
        run = _approval_step_run()
        invoked = next(tok for tok in run.split() if tok.endswith(_DRIVER))
        assert (_REPO_ROOT / invoked.removeprefix(".sdk-scripts/")).is_file()
