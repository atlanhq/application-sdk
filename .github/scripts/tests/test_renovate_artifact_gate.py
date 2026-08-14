"""Behavioural guard for the renovate/artifacts approval condition (FND-362).

`renovate-auto-approve-reusable.yml` withholds the atlan-ci code-owner approval
unless Renovate's own `renovate/artifacts` commit status is green. That gate is
the only thing stopping a failed post-upgrade task from auto-merging: Renovate
commits and raises the PR regardless, and platform automerge never consults
`artifactErrors`, so a failed release-age-bounded `uv lock` would otherwise land
an unbounded lock file unattended.

The classification is one `gh api --jq` line, and every interesting failure mode
lives inside it — an empty match must read as "missing" (jq emits nothing for
`first` over an empty array unless the `// "missing"` default catches it), and a
non-2xx response must not be parsed as a state. A textual assertion would prove
the line is present but not that it classifies correctly, so these tests lift the
real line out of the YAML and execute it against a stubbed `gh`.

Requires `jq` and `bash`, both present on the runners this suite targets.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = _REPO_ROOT / ".github/workflows/renovate-auto-approve-reusable.yml"

_GREEN = '{"statuses":[{"context":"renovate/artifacts","state":"success"}]}'
_RED = '{"statuses":[{"context":"renovate/artifacts","state":"failure"}]}'
_PENDING = '{"statuses":[{"context":"renovate/artifacts","state":"pending"}]}'
_OTHER_ONLY = '{"statuses":[{"context":"ci/build","state":"success"}]}'
_NO_STATUSES = '{"statuses":[]}'
_API_ERROR = '{"message":"Not Found"}'


def _approval_run_block() -> str:
    """The shell body of the approval step."""
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    steps = workflow["jobs"]["renovate-auto-approve"]["steps"]
    for step in steps:
        run = step.get("run", "")
        if "gh pr review" in run:
            return run
    raise AssertionError("no step in the workflow posts a review")


def _artifact_state_snippet() -> str:
    """The real ARTIFACT_STATE assignment, lifted verbatim from the workflow."""
    block = _approval_run_block()
    match = re.search(
        r"^\s*ARTIFACT_STATE=\$\(gh api.*?\)\s*$",
        block,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "ARTIFACT_STATE assignment not found in the approval step"
    return match.group(0).strip()


#: Stub standing in for `gh api`. It must apply `--jq` itself, exactly as gh
#: does, or the test would exercise a filter the shipped command never runs —
#: and the jq filter is the part of this gate most able to be subtly wrong.
_GH_STUB = """#!/usr/bin/env bash
filter=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--jq" ]; then filter="$arg"; fi
  prev="$arg"
done
if [ -n "$filter" ]; then
  printf '%s' "$PAYLOAD" | jq -r "$filter"
  status=$?
else
  printf '%s' "$PAYLOAD"
  status=0
fi
if [ "$EXIT_CODE" != "0" ]; then exit "$EXIT_CODE"; fi
exit "$status"
"""


def _classify(tmp_path: Path, payload: str, exit_code: int = 0) -> str:
    """Run the lifted snippet with a stubbed `gh` and return ARTIFACT_STATE."""
    stub = tmp_path / "gh"
    stub.write_text(_GH_STUB)
    stub.chmod(0o755)

    script = tmp_path / "probe.sh"
    script.write_text(
        "set -euo pipefail\n"
        'REPO="owner/repo"\n'
        'EVAL_SHA="deadbeef"\n'
        f"{_artifact_state_snippet()}\n"
        'printf "%s" "$ARTIFACT_STATE"\n'
    )

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin:/usr/local/bin",
            "PAYLOAD": payload,
            "EXIT_CODE": str(exit_code),
        },
        check=True,
    )
    return result.stdout.strip()


class TestClassification:
    def test_green_status_classifies_as_success(self, tmp_path):
        assert _classify(tmp_path, _GREEN) == "success"

    def test_red_status_is_not_success(self, tmp_path):
        assert _classify(tmp_path, _RED) == "failure"

    def test_pending_status_is_not_success(self, tmp_path):
        assert _classify(tmp_path, _PENDING) == "pending"

    def test_absent_context_reads_as_missing(self, tmp_path):
        # The transition case: branches created before statusCheckWhen=always,
        # and any branch Renovate has not committed to. Must NOT read as green.
        assert _classify(tmp_path, _OTHER_ONLY) == "missing"

    def test_empty_status_list_reads_as_missing(self, tmp_path):
        assert _classify(tmp_path, _NO_STATUSES) == "missing"

    def test_api_error_body_reads_as_missing(self, tmp_path):
        # A non-2xx response writes a JSON error object to stdout; parsing it as
        # a status list must not silently yield something that compares equal to
        # "success".
        assert _classify(tmp_path, _API_ERROR, exit_code=1) == "missing"

    @pytest.mark.parametrize(
        "payload,exit_code",
        [
            (_RED, 0),
            (_PENDING, 0),
            (_OTHER_ONLY, 0),
            (_NO_STATUSES, 0),
            (_API_ERROR, 1),
        ],
    )
    def test_nothing_but_a_green_status_compares_equal_to_success(
        self, tmp_path, payload, exit_code
    ):
        assert _classify(tmp_path, payload, exit_code) != "success"


class TestGateWiring:
    def test_gate_fails_closed_on_anything_but_success(self):
        block = _approval_run_block()
        assert '[ "$ARTIFACT_STATE" != "success" ]' in block, (
            "the gate must compare against success (fail closed); an "
            "`= failure` style check would approve on a missing status"
        )

    def test_gate_skips_the_pr_rather_than_falling_through(self):
        block = _approval_run_block()
        parts = block.split('[ "$ARTIFACT_STATE" != "success" ]', 1)
        assert len(parts) == 2, "the fail-closed comparison is missing entirely"
        body = parts[1].split("fi", 1)[0]
        assert "continue" in body, (
            "the gate must `continue` to the next PR when the status is not "
            "green; falling through would approve it anyway"
        )

    def test_gate_runs_before_the_approval_is_posted(self):
        block = _approval_run_block()
        assert block.index("ARTIFACT_STATE=") < block.index("gh pr review")

    def test_preset_publishes_the_status_on_healthy_branches(self):
        # The gate treats a missing context as not-green, which is only safe
        # because the fleet preset flips statusCheckWhen.artifactError to
        # "always". Renovate's default ("failed") publishes the context only on
        # error, and every healthy branch would stall unapproved.
        preset = json.loads((_REPO_ROOT / "renovate-config/default.json").read_text())
        assert preset["statusCheckWhen"]["artifactError"] == "always"
