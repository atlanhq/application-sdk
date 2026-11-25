"""Drift guard for the Renovate auto-approve fan-in (FND-3317).

Each caller fires the reusable once per Renovate SHA: its job-level ``if:``
admits only the anchor workflow's completion (or a re-run), and the reusable
waits out any required check still pending. The wiring lives in YAML
expressions, so nothing fails loudly when it breaks — renaming the anchor
workflow, or dropping it from the trigger list, silently stops every
auto-approval in the repo. These tests pin the pieces to each other.
"""

from __future__ import annotations

import re

import pytest
import renovate_approval_conditions as gate
from _workflows import WORKFLOWS, job, load_workflow, steps, triggers

_REUSABLE = WORKFLOWS / "renovate-auto-approve-reusable.yml"

# (caller, the workflow file the anchor name must belong to). application-sdk is
# the only caller: the app repos' bootstrap copy is retired.
_CALLERS = [
    pytest.param(
        WORKFLOWS / "renovate-auto-approve.yml",
        WORKFLOWS / "sdk-gate.yaml",
        id="application-sdk",
    ),
]

_ANCHOR_RE = re.compile(r"github\.event\.workflow_run\.name == '([^']+)'")
# The anchor workflow is read by its top-level `name:` line rather than parsed as YAML.
_NAME_RE = re.compile(r"^name:\s*[\"']?(.+?)[\"']?\s*$", re.MULTILINE)


@pytest.mark.parametrize("caller,anchor_workflow", _CALLERS)
class TestCallerFanIn:
    def test_anchor_is_a_listed_trigger_and_names_the_real_workflow(
        self, caller, anchor_workflow
    ):
        doc = load_workflow(caller)
        anchors = _ANCHOR_RE.findall(job(doc, "auto-approve")["if"])
        assert len(anchors) == 1, f"{caller.name}: expected exactly one anchor"
        anchor = anchors[0]
        assert anchor in triggers(doc)["workflow_run"]["workflows"]
        names = _NAME_RE.findall(anchor_workflow.read_text())
        assert names[:1] == [anchor]

    def test_reruns_still_re_evaluate(self, caller, anchor_workflow):
        condition = job(load_workflow(caller), "auto-approve")["if"]
        assert "github.event.workflow_run.run_attempt > 1" in condition
        assert "github.event.workflow_run.conclusion == 'success'" in condition
        assert "github.event_name == 'workflow_dispatch'" in condition

    def test_anchored_caller_asks_the_reusable_to_wait(self, caller, anchor_workflow):
        wait = job(load_workflow(caller), "auto-approve")["with"]["checks_wait_minutes"]
        assert 0 < wait * 60 <= gate.MAX_CHECKS_WAIT_SECONDS


class TestReusable:
    def test_wait_defaults_off_for_callers_that_still_fire_per_workflow(self):
        inputs = triggers(load_workflow(_REUSABLE))["workflow_call"]["inputs"]
        assert inputs["checks_wait_minutes"]["default"] == 0

    def test_wait_reaches_the_gate_script(self):
        envs = [
            s.get("env", {})
            for s in steps(job(load_workflow(_REUSABLE), "renovate-auto-approve"))
            if "run" in s
        ]
        assert any(
            e.get("CHECKS_WAIT_MINUTES") == "${{ inputs.checks_wait_minutes }}"
            for e in envs
        )

    def test_job_timeout_covers_the_longest_wait(self):
        approve = job(load_workflow(_REUSABLE), "renovate-auto-approve")
        # 10 minutes for the conditions themselves (the resync render is the
        # slow path) on top of the longest wait the script will honour.
        assert approve["timeout-minutes"] >= 10 + gate.MAX_CHECKS_WAIT_SECONDS // 60
