"""Tests for the completed-but-silent SDK review gate (FND-635).

Three runs on 2026-08-19 finished `status=completed`, billed up to $7.98, and
posted nothing to the PR — and the workflow exited 0 every time. These cover
both directions of the fix: a completed run with no in-window verdict must fail
the job, and the pre-existing soft-success path (verdict posted, stream broke
afterwards) must keep passing.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

SPEC = importlib.util.spec_from_file_location(
    "sdk_review_verdict_gate",
    Path(__file__).resolve().parents[1] / "sdk_review_verdict_gate.py",
)
verdict_gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verdict_gate)

WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github" / "workflows" / "sdk-review.yml"
)

# The starter step stamps `new Date().toISOString()` — milliseconds included.
# The REST API's created_at has none. The gate must compare them correctly.
STARTED_AT = "2026-08-19T18:56:03.371Z"
BEFORE = "2026-08-19T17:10:00Z"
AFTER = "2026-08-19T19:04:11Z"


HEAD = "0cab6b6e4eff94f28f249e1319ab74d55e1f7abc"
OTHER_HEAD = "51c160b06a2a350289c7d779f4ab887503f98685"


def summary_comment(
    created_at: str,
    marker: str = "<!-- SDK_REVIEW -->",
    head: str = HEAD,
) -> dict:
    return {
        "created_at": created_at,
        "body": (
            f"{marker}\n"
            "<!-- VERDICT: READY_TO_MERGE -->\n"
            f"<!-- REVIEWED_HEAD: {head} -->\n"
            "## SDK Review (mothership)\n"
        ),
    }


def starter_comment(created_at: str) -> dict:
    return {
        "created_at": created_at,
        "body": "<!-- SDK_REVIEW_STARTED -->\n🔍 **SDK Review (mothership)** triggered.",
    }


class FakeGh:
    """Records `gh` invocations and replays canned comment listings."""

    def __init__(
        self,
        comments: list[dict] | None = None,
        list_exit: int = 0,
        stdout: str | None = None,
        post_exit: int = 0,
    ) -> None:
        self.comments = comments if comments is not None else []
        self.list_exit = list_exit
        self.stdout = stdout
        self.post_exit = post_exit
        self.list_calls = 0
        self.posted: list[str] = []

    def __call__(self, args, **_kwargs) -> subprocess.CompletedProcess:
        if "-X" in args and "POST" in args:
            body = next(a[len("body=") :] for a in args if a.startswith("body="))
            self.posted.append(body)
            return subprocess.CompletedProcess(args, self.post_exit, "", "boom")
        self.list_calls += 1
        if self.list_exit != 0:
            return subprocess.CompletedProcess(args, self.list_exit, "", "HTTP 502")
        # --slurp wraps each page's array in an outer array.
        out = self.stdout if self.stdout is not None else json.dumps([self.comments])
        return subprocess.CompletedProcess(args, 0, out, "")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Base environment for a completed run on PR #3284."""
    output = tmp_path / "gha_output"
    output.write_text("")
    for key, value in {
        "REPO": "atlanhq/application-sdk",
        "PR_NUMBER": "3284",
        "FINAL_STATUS": "completed",
        "FINAL_COST": "7.98",
        "STARTER_STARTED_AT": STARTED_AT,
        "HEAD_SHA": HEAD,
        "GHA_RUN_URL": "https://github.com/atlanhq/application-sdk/actions/runs/32285618316",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    return output


def outputs(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
    )


# --- the defect this gate exists for -------------------------------------


def test_completed_with_no_summary_fails(env: Path):
    gh = FakeGh(comments=[starter_comment(STARTED_AT)])

    assert verdict_gate.main(gh, lambda _s: None) == 1
    assert outputs(env)["verdict_delivered"] == "false"
    assert outputs(env)["summary_count"] == "0"


def test_zero_is_confirmed_before_the_job_is_failed(env: Path):
    """The listing is not read-after-write consistent, so a single empty read
    is not proof. Re-read before turning silence into a red check."""
    gh = FakeGh(comments=[])

    assert verdict_gate.main(gh, lambda _s: None) == 1
    assert gh.list_calls == verdict_gate.RECHECK_ATTEMPTS


def test_a_late_arriving_summary_is_picked_up_by_the_recheck(env: Path):
    reads: list[int] = []

    def late(args, **_kwargs):
        reads.append(1)
        payload = [] if len(reads) == 1 else [summary_comment(AFTER)]
        return subprocess.CompletedProcess(args, 0, json.dumps([payload]), "")

    assert verdict_gate.main(late, lambda _s: None) == 0
    assert len(reads) == 2


def test_failure_posts_a_comment_naming_the_retry(env: Path):
    gh = FakeGh(comments=[])

    verdict_gate.main(gh, lambda _s: None)

    assert len(gh.posted) == 1
    body = gh.posted[0]
    assert body.startswith(verdict_gate.NO_VERDICT_MARKER)
    assert "@sdk-review" in body
    assert "7.98" in body
    # Must never look like a verdict to the counters or to the approver.
    assert "<!-- SDK_REVIEW -->" not in body


def test_summary_from_a_previous_trigger_does_not_count(env: Path):
    """The window bound is what stops an older head's verdict vouching for this run."""
    gh = FakeGh(comments=[summary_comment(BEFORE)])

    assert verdict_gate.main(gh, lambda _s: None) == 1
    assert outputs(env)["summary_count"] == "0"


# --- the inverse: delivered reviews stay green ---------------------------


def test_completed_with_an_in_window_summary_passes(env: Path):
    gh = FakeGh(comments=[starter_comment(STARTED_AT), summary_comment(AFTER)])

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert outputs(env)["verdict_delivered"] == "true"
    assert gh.posted == []


def test_mixed_precision_timestamps_are_compared_as_instants(env: Path):
    """`created_at` has second precision, `started_at` milliseconds. Compared
    as strings, "…:03Z" sorts AFTER "…:03.371Z" ('Z' > '.') and a pre-window
    comment would vouch for this run; compared as instants it does not."""
    gh = FakeGh(comments=[summary_comment("2026-08-19T18:56:03Z")])
    assert verdict_gate.main(gh, lambda _s: None) == 1

    gh = FakeGh(comments=[summary_comment("2026-08-19T18:56:04Z")])
    assert verdict_gate.main(gh, lambda _s: None) == 0


def test_a_summary_for_another_head_does_not_vouch_for_this_run(env: Path):
    """The narrowing this gate was dropping.

    A footerless summary inside our window, for a head this run was not
    dispatched for — a zombie sandbox still posting for the sha it was
    reviewing. It counted here while the dedupe step, passing the same
    `head_sha` to the same shared decision, called it nobody's. The gate is the
    one that exits non-zero, so it has to be at least as strict.
    """
    gh = FakeGh(comments=[summary_comment(AFTER, head=OTHER_HEAD)])

    assert verdict_gate.main(gh, lambda _s: None) == 1
    assert outputs(env)["verdict_delivered"] == "false"


def test_our_own_head_in_the_window_still_vouches(env: Path):
    """The inverse, so the narrowing cannot be over-tightened unnoticed."""
    gh = FakeGh(comments=[summary_comment(AFTER)])

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert outputs(env)["verdict_delivered"] == "true"


def test_legacy_test_marker_counts_as_delivered(env: Path):
    gh = FakeGh(comments=[summary_comment(AFTER, "<!-- TEST_SDK_REVIEW -->")])

    assert verdict_gate.main(gh, lambda _s: None) == 0


def test_non_completed_status_is_left_to_the_dispatch_step(
    env: Path, monkeypatch: pytest.MonkeyPatch
):
    """Stream broke after the verdict was posted → the dispatch step's
    soft-success rule owns it, and this gate must not re-decide it."""
    monkeypatch.setenv("FINAL_STATUS", "error")
    gh = FakeGh(comments=[])

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert gh.list_calls == 0
    assert gh.posted == []
    assert outputs(env)["verdict_delivered"] == "unknown"


# --- fail-open paths ------------------------------------------------------


def test_unreadable_comment_list_fails_open_after_retries(env: Path):
    gh = FakeGh(list_exit=1)

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert gh.list_calls == verdict_gate.FETCH_ATTEMPTS
    assert outputs(env)["verdict_delivered"] == "unknown"
    assert gh.posted == []


def test_transient_list_failure_is_retried_then_succeeds(env: Path):
    calls: list[int] = []

    def flaky(args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
        return subprocess.CompletedProcess(
            args, 0, json.dumps([[summary_comment(AFTER)]]), ""
        )

    assert verdict_gate.main(flaky, lambda _s: None) == 0
    assert outputs(env)["verdict_delivered"] == "true"


def test_unparseable_payload_fails_open(env: Path):
    gh = FakeGh(stdout="not json")

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert outputs(env)["verdict_delivered"] == "unknown"


def test_missing_window_bound_fails_open(env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("STARTER_STARTED_AT", "")
    gh = FakeGh(comments=[])

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert gh.list_calls == 0
    assert outputs(env)["verdict_delivered"] == "unknown"


def test_missing_pr_number_fails_open(env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PR_NUMBER", "")
    gh = FakeGh(comments=[])

    assert verdict_gate.main(gh, lambda _s: None) == 0
    assert outputs(env)["verdict_delivered"] == "unknown"


def test_unpostable_comment_still_fails_the_job(env: Path):
    """The red check is the primary signal; a failed comment must not mask it."""
    gh = FakeGh(comments=[], post_exit=1)

    assert verdict_gate.main(gh, lambda _s: None) == 1


# --- workflow wiring ------------------------------------------------------


def dispatch_job() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["sdk-review-dispatch"]


def test_workflow_runs_the_gate_after_dispatch():
    steps = dispatch_job()["steps"]
    names = [s.get("name", "") for s in steps]
    gate = next(s for s in steps if s.get("id") == "verdict")

    assert "sdk_review_verdict_gate.py" in gate["run"]
    assert names.index("Verify the review delivered a verdict") > names.index(
        "Dispatch to mothership Rover Direct API"
    )
    # Without the window bound and the terminal status the gate can only fail
    # open, which would make it decorative.
    for key in ("PR_NUMBER", "FINAL_STATUS", "STARTER_STARTED_AT", "GH_TOKEN"):
        assert key in gate["env"]


def test_gate_precedes_the_approval_so_a_silent_run_cannot_be_stamped():
    steps = dispatch_job()["steps"]
    names = [s.get("name", "") for s in steps]
    approve = names.index("Approve PR as atlan-ci (counts as code-owner)")

    assert names.index("Verify the review delivered a verdict") < approve
    # `success()` is what makes the ordering load-bearing: a failed gate must
    # skip the approval rather than merely precede it.
    assert steps[approve]["if"].startswith("success()")


def test_stamp_step_consumes_the_gate_output():
    """The gate's output only reaches the PR through this env line. Drop it and
    a completed-but-silent run stamps '✅ Completed' again — the exact
    reassurance this change exists to remove."""
    stamp = next(
        s
        for s in dispatch_job()["steps"]
        if s.get("name") == "Stamp cost + status onto starter comment"
    )

    assert (
        stamp["env"]["VERDICT_DELIVERED"]
        == "${{ steps.verdict.outputs.verdict_delivered }}"
    )


def test_stamp_step_switches_wording_on_the_exact_gate_string():
    """'false' is the only value the gate emits for a silent run — 'unknown'
    and '' mean it fell open or never ran. An inverted or loosened comparison
    would either re-hide the failure or red-flag every healthy review."""
    stamp = next(
        s
        for s in dispatch_job()["steps"]
        if s.get("name") == "Stamp cost + status onto starter comment"
    )
    script = stamp["with"]["script"]

    assert "const noVerdict = process.env.VERDICT_DELIVERED === 'false';" in script
    # The no-verdict verb must be chosen ahead of the two '✅ Completed'
    # branches, which both match a completed-but-silent run.
    assert script.index("noVerdict ? '🟥") < script.index("'✅ **Completed**'")
    assert "posted no verdict" in script
    assert "Re-tag" in script


def test_soft_success_rule_is_still_intact():
    """The delivered-then-dropped case (run 29001242204) must keep passing:
    `fail_or_warn` still downgrades to a warning when a verdict was posted.

    The rule moved out of inlined shell and into `sdk_review_dispatch.py`
    (FND-643), where `test_sdk_review_dispatch.py` exercises every failure
    branch against it. This keeps asserting from the gate's side that the
    dispatch step is still the thing that owns the rule — the gate and the
    dispatch step cover disjoint cases, and that only holds while both exist.
    """
    dispatch = next(s for s in dispatch_job()["steps"] if s.get("id") == "dispatch")[
        "run"
    ]
    assert "sdk_review_dispatch.py" in dispatch

    driver = (
        Path(__file__).resolve().parents[1] / "sdk_review_dispatch.py"
    ).read_text()
    assert "def fail_or_warn(msg: str) -> bool:" in driver
    assert "if verdict_posted:" in driver
    assert "already posted on PR #" in driver
