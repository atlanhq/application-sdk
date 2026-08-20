"""Workflow-side premises the dedupe logic in sdk_review_gate.py depends on.

The Python is only as correct as the YAML that feeds it: the locked phase reads
stamps that `sdk-review.yml` has to write, and its `skip` decision is only
cheap if every downstream step is actually guarded on it. Both are one careless
edit away from silently regressing, so they are asserted here rather than left
to review.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github/workflows/sdk-review.yml"

SPEC = importlib.util.spec_from_file_location(
    "sdk_review_gate", REPO_ROOT / ".github/scripts/sdk_review_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)

GUARD = "steps.lock_gate.outputs.decision != 'skip'"

# Steps that are inert on a skip without naming the guard, and why. Anything
# outside this set must carry GUARD.
INERT_ON_SKIP = {
    # Run before the check, and feed it.
    "Checkout repo (for local globalprotect-connect action)": "runs before the check",
    "Parse comment intent": "runs before the check",
    "Dedupe check (authoritative — inside the concurrency lock)": "is the check",
    # The check's own skip path.
    "React to skipped re-trigger": "only fires on skip",
    # Chained off a guarded step's outcome, so a skip propagates.
    "Connect to VPN (attempt 2/3)": "chained off vpn1.outcome",
    "Connect to VPN (attempt 3/3)": "chained off vpn2.outcome",
    "Notify PR if VPN failed after 3 attempts": "chained off vpn3.outcome",
    "Fail workflow after VPN failure notification": "chained off vpn3.outcome",
    # Gated on an output a guarded step never produced.
    "Terminate mothership sandbox on cancel": "needs session.outputs.session_id",
    "Stamp cost + status onto starter comment": "needs starter.outputs.comment_id",
    "Set sdk-review status to failure (only on workflow failure)": (
        "needs pr.outputs.head_sha"
    ),
}


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def dispatch_steps() -> list[dict]:
    return workflow()["jobs"]["sdk-review-dispatch"]["steps"]


def step(name: str) -> dict:
    for candidate in dispatch_steps():
        if candidate.get("name") == name:
            return candidate
    raise AssertionError(f"no step named {name!r} in sdk-review-dispatch")


def starter_script() -> str:
    return step("Post 'review starting' notice to PR")["with"]["script"]


# --- the check runs under the lock, ahead of every side effect ------------


def test_the_check_is_in_the_locked_job():
    job = workflow()["jobs"]["sdk-review-dispatch"]
    assert "sdk-review-" in job["concurrency"]["group"]
    assert job["concurrency"]["cancel-in-progress"] is False
    assert any(s.get("id") == "lock_gate" for s in job["steps"])


def test_the_check_runs_in_the_locked_phase_and_knows_its_own_run_id():
    check = step("Dedupe check (authoritative — inside the concurrency lock)")
    assert check["env"]["GATE_PHASE"] == "locked"
    assert check["env"]["RUN_ID"] == "${{ github.run_id }}"


def test_a_crashed_check_reviews_rather_than_dropping_the_trigger():
    """No decision output means "not skip", so the guards below let it run."""
    check = step("Dedupe check (authoritative — inside the concurrency lock)")
    assert check["continue-on-error"] is True


def test_the_preflight_gate_declares_its_phase():
    assert workflow()["jobs"]["gate"]["steps"][1]["env"]["GATE_PHASE"] == "preflight"


def test_the_check_precedes_every_side_effect():
    names = [s.get("name") for s in dispatch_steps()]
    check = names.index("Dedupe check (authoritative — inside the concurrency lock)")
    for effect in (
        "React to comment",
        "Ensure labels exist",
        "Post 'review starting' notice to PR",
        "Set sdk-review status to pending",
        "Dispatch to mothership Rover Direct API",
    ):
        assert names.index(effect) > check, f"{effect} runs before the dedupe check"


def test_every_step_is_either_guarded_or_provably_inert():
    unguarded = [
        s.get("name")
        for s in dispatch_steps()
        if GUARD not in str(s.get("if", "")) and s.get("name") not in INERT_ON_SKIP
    ]
    assert unguarded == [], (
        "new step(s) in sdk-review-dispatch neither carry the lock_gate guard nor "
        f"are listed as inert on skip: {unguarded}"
    )


# --- the starter comment writes what the locked phase reads ---------------


def test_the_starter_stamps_the_head_and_run_the_check_looks_for():
    script = starter_script()
    assert "<!-- SDK_REVIEW_STARTED_HEAD: ${process.env.HEAD_SHA} -->" in script
    assert "<!-- SDK_REVIEW_STARTED_RUN: ${process.env.RUN_ID} -->" in script
    assert "headStamp," in script and "runStamp," in script


def test_the_starter_is_given_the_head_and_run_id():
    env = step("Post 'review starting' notice to PR")["env"]
    assert env["HEAD_SHA"] == "${{ steps.pr.outputs.head_sha }}"
    assert env["RUN_ID"] == "${{ github.run_id }}"


def test_the_marker_stays_on_the_first_line_so_folding_still_matches():
    """The fold logic keys off `body.startsWith(marker)`; stamps go after it."""
    script = starter_script()
    body = script.split("const body = [", 1)[1]
    assert body.lstrip().startswith("marker,")


def test_the_stamp_the_check_treats_as_finished_is_the_one_the_stamper_writes():
    """`STARTER_STAMP` and the stamper's idempotence guard must not drift.

    If the JS footer changes shape, every finished run starts looking in-flight
    and bot re-triggers get skipped forever.
    """
    stamper = step("Stamp cost + status onto starter comment")["with"]["script"]
    assert f"includes('{gate.STARTER_STAMP}')" in stamper
    assert f"{gate.STARTER_STAMP}" + "${finalStatus}" in stamper.replace("\\`", "`")


def test_the_stamper_runs_even_on_a_cancel():
    """The check reads "no stamp" as "still running", so this must be always()."""
    assert step("Stamp cost + status onto starter comment")["if"].startswith("always()")


# --- the verdict-side collapse is wired into the dispatch step ------------


def test_the_dispatch_step_collapses_duplicate_verdicts():
    """The dispatch driver runs the dedupe pass before it decides pass/fail.

    Since FND-643 the dispatch step is `sdk_review_dispatch.py` rather than
    inlined shell, so the wiring lives in Python: it calls the dedupe module
    with SINCE bound to the starter timestamp and reads `verdict_posted` back
    for the soft-success rule.
    """
    run = step("Dispatch to mothership Rover Direct API")["run"]
    assert "sdk_review_dispatch.py" in run
    driver = (REPO_ROOT / ".github/scripts/sdk_review_dispatch.py").read_text()
    assert "import sdk_review_dedupe_verdicts" in driver
    assert 'os.environ["SINCE"] = since' in driver
    assert 'os.environ.get("STARTER_STARTED_AT", "")' in driver
    assert "verdict_posted" in driver


def test_the_dispatch_step_supplies_the_ownership_key():
    """Attribution is by run URL; HEAD_SHA and SINCE only feed the fallback."""
    env = step("Dispatch to mothership Rover Direct API")["env"]
    assert env["GHA_RUN_URL"].endswith("/actions/runs/${{ github.run_id }}")
    assert env["HEAD_SHA"] == "${{ steps.pr.outputs.head_sha }}"
    assert env["STARTER_STARTED_AT"] == "${{ steps.starter.outputs.started_at }}"


def test_the_delivery_gate_shares_the_ownership_key():
    """The gate that reds a run and the step that hides duplicates must agree.

    Both read `sdk_review_summaries.attribute()`; the gate can only reach the
    exact tier if the workflow hands it the same run URL.
    """
    env = step("Verify the review delivered a verdict")["env"]
    assert env["GHA_RUN_URL"].endswith("/actions/runs/${{ github.run_id }}")
    assert env["STARTER_STARTED_AT"] == "${{ steps.starter.outputs.started_at }}"
    # Every narrowing attribute() offers, or the two steps answer differently
    # for the same comment — and this is the one that exits non-zero.
    assert env["HEAD_SHA"] == "${{ steps.pr.outputs.head_sha }}"


def test_both_attribution_callers_are_given_the_same_inputs():
    """A shared decision only agrees if both callers feed it the same thing.

    The dedupe step reads these from its own env; the gate step from its. A
    narrowing wired into one and not the other is how the shared module ends up
    returning two answers for one comment.
    """
    shared = ("GHA_RUN_URL", "HEAD_SHA")
    dispatch = step("Dispatch to mothership Rover Direct API")["env"]
    verdict = step("Verify the review delivered a verdict")["env"]
    for key in shared:
        assert dispatch[key] == verdict[key], f"{key} differs between the two callers"


def test_the_summary_template_still_carries_the_run_url():
    """The ownership key only works because §3e mandates this footer line.

    If the template ever drops it, every run falls back to window attribution
    and stops collapsing duplicates — silently. Fail here instead.
    """
    orchestration = (REPO_ROOT / ".mothership/pr-review/ORCHESTRATION.md").read_text()
    assert "**Run:** [view workflow logs + cost](<GHA_RUN_URL>)" in orchestration
    assert "The trailing **Run:** line is required on every summary." in orchestration
