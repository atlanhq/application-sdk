"""A review-only run: one review, no fixes, no approval — on a PR in any state.

The A/B measures the new lane against the reviews PRs actually received, so
it has to run on merged PRs. Everything the full loop does after Review 1
exists to change the branch — prep pushes, the resolver pushes, the approval
workflow approves. Every one of those is wrong on a merged PR, so every one
of them must provably not run. These tests are that proof, lifted from the
generated workflow and the real scripts rather than from the intent.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import gen_sdk_loop_workflow as gen  # noqa: E402
import sdk_loop_finalize as finalize  # noqa: E402
import sdk_loop_live as live  # noqa: E402
import sdk_loop_refute as refute  # noqa: E402
import sdk_review_approve as approve  # noqa: E402
from _gha_expr import evaluate  # noqa: E402
from sdk_loop_fence import admit_state, start_comment  # noqa: E402
from sdk_loop_findings import MARK_AB, load_severity, render_markers  # noqa: E402
from sdk_loop_pack import build_pack  # noqa: E402
from sdk_loop_routing import load_routing  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github/workflows/sdk-loop.yml"
PHASE_WORKFLOW = REPO / ".github/workflows/sdk-loop-phase.yml"
SHA = "a" * 40


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _ctx(review_only: str, **outcomes: str) -> dict:
    """A run where the fence admitted the PR and Review 1 found work."""
    needs = {
        "fence": {"outputs": {"proceed": "true", "review_only": review_only}},
        "prep": {"outputs": {"outcome": ""}},
        "review-1": {"outputs": {"outcome": "ok"}},
    }
    for job, outcome in outcomes.items():
        needs[job] = {"outputs": {"outcome": outcome}}
    return {"needs": needs}


# ---------------------------------------------------------------------------
# The fence admits a merged PR only for a review-only run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_the_full_loop_still_refuses_a_pr_that_is_not_open(state: str) -> None:
    reason = admit_state(state, review_only=False)
    assert reason and state.lower() in reason


@pytest.mark.parametrize("state", ["MERGED", "CLOSED", "OPEN"])
def test_a_review_only_run_admits_any_state(state: str) -> None:
    assert admit_state(state, review_only=True) is None


def test_an_open_pr_is_admitted_either_way() -> None:
    assert admit_state("OPEN", review_only=False) is None


def test_the_start_comment_says_nothing_will_change() -> None:
    """The author of a merged PR sees a bot start on it. The first line must
    say the run is a measurement, before any verdict arrives to alarm them."""
    body = start_comment("42", "http://x/runs/9", SHA, review_only=True)
    assert "review-only" in body
    assert "no fixes" in body and "no approval" in body
    full = start_comment("42", "http://x/runs/9", SHA)
    assert "review-only" not in full


# ---------------------------------------------------------------------------
# The chain stops after Review 1 — proven on the generated gates
# ---------------------------------------------------------------------------


def test_the_fence_exports_the_flag_the_gates_read() -> None:
    jobs = _jobs()
    assert "review_only" in jobs["fence"]["outputs"]
    env = jobs["fence"]["steps"][-2]["env"]  # the fence step, before the reaction
    assert env["REVIEW_ONLY"] == "${{ inputs.review_only }}"


def test_the_dispatch_input_exists_and_defaults_off() -> None:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    inp = wf[True]["workflow_dispatch"]["inputs"]["review_only"]  # `on:` parses as True
    assert inp["type"] == "boolean"
    assert inp["default"] is False


def test_review_one_runs_on_a_review_only_run() -> None:
    ctx = _ctx("true")
    assert evaluate(_jobs()["review-1"]["if"], ctx) is True


def test_prep_does_not_run_on_a_review_only_run() -> None:
    """Prep pushes and its fast track cancels the review. Both are wrong on a
    PR nobody is driving."""
    assert evaluate(_jobs()["prep"]["if"], _ctx("true")) is False
    assert evaluate(_jobs()["prep"]["if"], _ctx("false")) is True


def test_nothing_after_review_one_runs_on_a_review_only_run() -> None:
    """Every resolve and every later review, with the outcomes that would
    normally continue the chain. The resolver is what pushes; if any of these
    evaluates true the A/B can rewrite a merged branch."""
    jobs = _jobs()
    ctx = _ctx("true", **{f"resolve-{n}": "ok" for n in range(1, gen.MAX_ROUNDS + 1)})
    for n in range(1, gen.MAX_ROUNDS + 1):
        ctx["needs"][f"review-{n}"] = {"outputs": {"outcome": "ok"}}
    for n in range(1, gen.MAX_ROUNDS + 1):
        assert evaluate(jobs[f"resolve-{n}"]["if"], ctx) is False, f"resolve-{n}"
        if n > 1:
            assert evaluate(jobs[f"review-{n}"]["if"], ctx) is False, f"review-{n}"


def test_the_full_loop_is_unchanged_when_the_flag_is_off() -> None:
    jobs = _jobs()
    ctx = _ctx("false", **{"resolve-1": "ok"})
    assert evaluate(jobs["resolve-1"]["if"], ctx) is True
    assert evaluate(jobs["review-2"]["if"], ctx) is True


def test_a_fence_that_predates_the_flag_still_loops() -> None:
    """An empty output — a fence from before this existed — must read as the
    full loop, not as review-only. `!= 'true'`, never `== 'false'`."""
    jobs = _jobs()
    ctx = _ctx("", **{"resolve-1": "ok"})
    assert evaluate(jobs["resolve-1"]["if"], ctx) is True
    assert evaluate(jobs["review-2"]["if"], ctx) is True
    assert evaluate(jobs["prep"]["if"], ctx) is True


def test_the_flag_reaches_the_review_phase() -> None:
    """The verdict marker is rendered inside the phase, so the phase has to be
    told. Round 1 passes the fence's flag; the phase declares the input and
    exports it to the script."""
    review_1 = _jobs()["review-1"]["with"]
    assert "needs.fence.outputs.review_only" in review_1["review_only"]
    phase = yaml.safe_load(PHASE_WORKFLOW.read_text(encoding="utf-8"))
    assert "review_only" in phase[True]["workflow_call"]["inputs"]
    run_step = next(s for s in phase["jobs"]["phase"]["steps"] if s.get("id") == "run")
    assert run_step["env"]["REVIEW_ONLY"] == "${{ inputs.review_only }}"


def test_the_summary_names_the_mode_not_the_verdict() -> None:
    """Left to the round scan, a NEEDS_FIXES review would be reported as why
    the run stopped. The run stopped because it was asked to."""
    stop = _jobs()["finalize"]["steps"][-1]["env"]["STOP_REASON"]
    assert stop.index("review_only == 'true' && 'review_only'") < stop.index(
        "needs.review-1.outputs.outcome"
    )
    assert "review_only" in finalize.STOP_TEXT
    assert "no approval" in finalize.STOP_TEXT["review_only"]


# ---------------------------------------------------------------------------
# The verdict carries the marker, and the approval path stands down on it
# ---------------------------------------------------------------------------


def test_the_marker_is_last_and_additive() -> None:
    """Every existing parser reads its marker by regex. The A/B marker is a
    trailing line, so a review-only verdict parses exactly as before for
    everyone who does not know about it."""
    plain = render_markers("NEEDS_FIXES", SHA)
    flagged = render_markers("NEEDS_FIXES", SHA, review_only=True)
    assert flagged.startswith(plain)
    assert flagged.splitlines()[-1] == MARK_AB
    assert MARK_AB not in plain


def test_deliver_stamps_the_marker_on_a_review_only_verdict() -> None:
    diff = (
        "diff --git a/application_sdk/x.py b/application_sdk/x.py\n"
        "--- a/application_sdk/x.py\n+++ b/application_sdk/x.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
    )
    pack = build_pack(repo=REPO, diff=diff, scope="full", routing=load_routing())
    payload = (
        '{"pack_id":"p","status":"complete","reviewed_files":["application_sdk/x.py"],'
        '"findings":[],"strengths":[],"notes":""}'
    )
    kw = dict(
        payload_text=payload,
        pack=pack,
        sev=load_severity(),
        by_design=None,
        challenge=None,
        challenge_brief="# Refuter",
        challenge_mode=refute.CROSS_FAMILY,
        diff=diff,
        redgreen_report=None,
        pr=1,
        pr_title="t",
        reviewed_head=SHA,
        answers_trigger=None,
        model="m",
        run_url="",
    )
    assert MARK_AB in live.deliver(**kw, review_only=True).body
    assert MARK_AB not in live.deliver(**kw).body


def test_the_approval_path_refuses_a_review_only_verdict(monkeypatch) -> None:
    """READY_TO_MERGE on a merged PR would otherwise label, approve and set a
    status. The guard runs before the verdict is read, so no later rule can
    be argued into acting."""
    calls: list[list[str]] = []

    def gh(argv, **_kw):
        calls.append(list(argv))
        raise AssertionError(f"approve reached gh: {argv}")

    for key, value in {
        "REPO": "o/r",
        "PR_NUMBER": "1",
        "COMMENT_BODY": render_markers("READY_TO_MERGE", SHA, review_only=True),
        "APPROVER_TOKEN": "t",
    }.items():
        monkeypatch.setenv(key, value)
    outcome = approve.stamp_verdict(runner=gh)
    assert outcome.action == approve.SKIPPED
    assert "review-only" in outcome.detail
    assert outcome.exit_code == 0
    assert calls == []


def test_the_approve_script_mirrors_the_marker_it_cannot_import() -> None:
    """sdk_review_approve runs on a bare runner and must not import the loop
    lane. The constant is duplicated; this is what keeps the copies equal."""
    assert approve.MARK_AB == MARK_AB
