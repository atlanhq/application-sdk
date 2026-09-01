"""Prep phase — branch and check hygiene the review cannot do itself."""

from __future__ import annotations

import pathlib
import re
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_prep  # noqa: E402
from sdk_loop_prep import (  # noqa: E402
    BUCKET_FAIL,
    CHECK_FIELDS,
    OUTCOME_CLEAN,
    OUTCOME_CONFLICTS,
    OUTCOME_RED,
    OUTCOME_UNKNOWN,
    decide,
    failing_checks,
    needs_agent,
    pr_state,
)


def test_a_healthy_pr_costs_no_model_call() -> None:
    """The common case must be free. Most PRs enter the loop current and
    green, and paying an agent to confirm that would reintroduce the exact
    waste this phase was built to take OUT of the review."""
    clean = decide({"mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}, (), "a" * 40)
    assert clean.outcome == OUTCOME_CLEAN
    assert clean.ci_state == "green"
    assert not needs_agent(clean), "a clean PR must not reach the model"
    assert clean.pushed_sha == "", "nothing was pushed, so claim nothing"
    assert "behind" not in clean.detail.lower()


def test_a_behind_branch_is_reported_not_updated() -> None:
    """Prep does not touch the branch on its own initiative.

    Merging base into somebody's PR is a change to their branch they did not
    ask for, and it is not needed to review: the review reads the diff
    against base, which is well-defined whether or not base has moved. So
    BEHIND is a line in the detail, not an action.
    """
    out = decide({"mergeStateStatus": "BEHIND", "headRefOid": "a" * 40}, (), "a" * 40)
    assert out.outcome == OUTCOME_CLEAN
    assert out.pushed_sha == "", "prep pushed nothing, so it must claim nothing"
    assert out.new_base_sha == "a" * 40, "the review runs on the real head"
    assert "behind" in out.detail.lower(), "a human should still be able to see it"
    assert not needs_agent(out)


def test_conflicts_are_reported_and_never_resolved() -> None:
    """A conflict resolution is the author's decision about their own change.
    That was true when the reviewer had no write scope and stays true now
    that prep does — a bot with push rights must not quietly merge for them."""
    out = decide(
        {"mergeStateStatus": "CONFLICTING", "headRefOid": "a" * 40}, (), "a" * 40
    )
    assert out.outcome == OUTCOME_CONFLICTS
    assert not needs_agent(out), "conflicts need a human, not an agent with write scope"
    assert "author" in out.detail


def test_red_checks_do_not_block_the_review() -> None:
    """Red CI is a fact to hand forward, not prep's problem to solve. Blocking
    would mean a broken check costs the review entirely — and the review is
    the thing that explains WHY it is broken."""
    out = decide(
        {"mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}, ("SDK Tests",), "a" * 40
    )
    assert out.outcome == OUTCOME_RED
    assert out.new_base_sha == "a" * 40, "the review still runs, on the real head"
    assert needs_agent(out), "red checks are the one case worth a model"


def test_prep_holds_write_scope_and_the_review_does_not() -> None:
    """The whole point. If prep were read-only it could not update a branch,
    and if review were writable its read-only contract would be a promise in
    a prompt rather than a property of its credential."""
    text = pathlib.Path(".github/workflows/sdk-loop-phase.yml").read_text(
        encoding="utf-8"
    )
    line = next(x for x in text.splitlines() if "permission-contents:" in x)
    assert "'prep'" in line and "'resolve'" in line
    assert "'write'" in line and "'read'" in line


def test_the_generated_chain_wires_prep_without_stranding_review_one() -> None:
    """A `needs.<job>` reference to a job the caller does not depend on makes
    GitHub refuse to parse the workflow — so NO jobs run and the fence never
    gets to say why. That silence is the failure this asserts against."""
    raw = pathlib.Path(".github/workflows/sdk-loop.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    jobs = doc["jobs"]
    assert "prep" in jobs

    for name, spec in jobs.items():
        declared = set(spec.get("needs") or [])
        refs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", yaml.dump(spec)))
        assert refs <= set(jobs), f"{name} references a job that does not exist"
        assert (
            refs <= declared
        ), f"{name} references an undeclared need: {refs - declared}"

    r1 = jobs["review-1"]
    assert set(r1["needs"]) == {"fence", "prep"}
    with_ = r1["with"]
    # Fallback to the fence: a skipped prep emits nothing, and an empty
    # base_sha would checkout `ref: ''`.
    assert "needs.fence.outputs.base_sha" in str(with_["base_sha"])
    assert "needs.prep.outputs.pushed_sha" in str(with_["ours"])
    # Review 1 must survive a prep that failed — an un-updated branch still
    # reviews correctly.
    assert "!cancelled()" in str(r1["if"])
    assert "prep.result" not in str(r1["if"])

    assert "prep" in jobs["finalize"]["needs"]


# ---------------------------------------------------------------------------
# The gh boundary — where the first version of this file was silently wrong
# ---------------------------------------------------------------------------


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_the_check_reader_asks_for_fields_gh_actually_has() -> None:
    """This file shipped asking for `conclusion`, which `gh pr checks --json`
    does not have. gh writes "Unknown JSON field" to stderr, EXITS 0, and
    prints nothing — so a reader that trusts stdout sees no failures and
    reports green on a red PR. Nothing anywhere raised.

    The tuple-injecting tests could not catch it: they start after the parse.
    This one pins the wire vocabulary, which is the only place the mistake
    was visible.
    """
    seen: list[list[str]] = []

    def runner(args: list[str]) -> _Done:
        seen.append(args)
        return _Done(0, "[]")

    failing_checks("o/r", 1, runner=runner)
    fields = seen[0][seen[0].index("--json") + 1].split(",")
    assert "conclusion" not in fields, "gh has no `conclusion` field on pr checks"
    assert set(fields) <= {
        "bucket",
        "completedAt",
        "description",
        "event",
        "link",
        "name",
        "startedAt",
        "state",
        "workflow",
    }, "asked gh for a field it does not expose — it will exit 0 and print nothing"
    assert "bucket" in fields, "bucket carries pass/fail; state/conclusion do not"


def test_a_failing_check_is_actually_detected() -> None:
    """The end-to-end shape, in gh's real vocabulary. #3575 had one `fail`
    bucket at the time this was written, and the shipped reader called it
    green."""
    payload = (
        '[{"name":"SDK Tests","state":"FAILURE","bucket":"fail"},'
        '{"name":"Conformance","state":"SUCCESS","bucket":"pass"},'
        '{"name":"E2E","state":"SKIPPED","bucket":"skipping"}]'
    )
    got = failing_checks("o/r", 1, runner=lambda a: _Done(0, payload))
    assert got == ("SDK Tests",)
    assert BUCKET_FAIL == "fail"
    assert "conclusion" not in CHECK_FIELDS

    out = decide({"mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}, got, "a" * 40)
    assert out.outcome == OUTCOME_RED
    assert needs_agent(out), "a real failure must reach the mechanical-fix path"


def test_an_unreadable_state_is_unknown_and_never_green() -> None:
    """Fail CLOSED. Every layer of the first version degraded into the most
    optimistic answer: gh exited 0, `stdout or "[]"` made empty look like an
    empty list, and an empty list looked like a healthy PR."""
    for bad in (_Done(1, ""), _Done(0, ""), _Done(0, "not json")):
        assert failing_checks("o/r", 1, runner=lambda a, b=bad: b) is None
        assert pr_state("o/r", 1, runner=lambda a, b=bad: b) is None

    # Shape-specific: a JSON object is not a check list, and a state object
    # with no headRefOid is not a state — both would otherwise read as
    # "nothing wrong".
    assert failing_checks("o/r", 1, runner=lambda a: _Done(0, '{"a":1}')) is None
    assert pr_state("o/r", 1, runner=lambda a: _Done(0, '{"a":1}')) is None
    assert pr_state("o/r", 1, runner=lambda a: _Done(0, '{"headRefOid":"abc"}')) == {
        "headRefOid": "abc"
    }

    unknown_checks = decide(
        {"mergeStateStatus": "CLEAN", "headRefOid": "a" * 40}, None, "a" * 40
    )
    assert unknown_checks.outcome == OUTCOME_UNKNOWN
    assert unknown_checks.ci_state != "green"

    unknown_state = decide(None, None, "a" * 40)
    assert unknown_state.outcome == OUTCOME_UNKNOWN
    assert unknown_state.new_base_sha == "a" * 40, "the review still runs"
    assert unknown_state.outcome != OUTCOME_CLEAN


def test_a_clean_prep_installs_nothing() -> None:
    """Prep is normally two `gh` reads and no model. It was still paying
    `npm install -g opencode` first — 18 seconds of a 41-second phase, every
    run, preparing for a branch it almost never takes.

    So the deterministic pass runs BEFORE the installs and gates them. This
    asserts the wiring, because the saving is invisible from the Python side:
    the script cannot tell whether the workflow installed anything.
    """
    wf = pathlib.Path(".github/workflows/sdk-loop-phase.yml").read_text(
        encoding="utf-8"
    )
    gate = "inputs.phase != 'prep' || steps.prep.outputs.needs_agent == 'true'"

    # The deterministic step must come first, or gating it is impossible.
    assert wf.index("Prep — deterministic pass") < wf.index("Install opencode")

    for step in ("Cache the opencode install", "Install opencode", "Install uv"):
        head = wf.index(f"- name: {step}")
        assert gate in wf[head : head + 400], f"{step} is not gated on needs_agent"

    # A skipped `run` step has empty outputs, so the job must fall back to the
    # deterministic step — otherwise a clean prep reports nothing and Review 1
    # checks out `ref: ''`.
    for key in ("outcome", "new_base_sha", "pushed_sha", "ci_state", "detail"):
        assert (
            f"steps.run.outputs.{key} || steps.prep.outputs.{key}" in wf
        ), f"job output {key} has no fallback for a deterministic prep"


def test_the_deterministic_cli_reports_whether_an_agent_is_wanted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """`needs_agent` is what the workflow gates on, so it has to be emitted
    even on the happy path — an absent output reads as false, which is right
    here, but only by accident. Assert it is written explicitly."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("BASE_SHA", "a" * 40)
    monkeypatch.setattr(
        sdk_loop_prep,
        "pr_state",
        lambda r, p, **k: {"mergeStateStatus": "CLEAN", "headRefOid": "a" * 40},
    )
    monkeypatch.setattr(sdk_loop_prep, "failing_checks", lambda r, p, **k: ())
    assert sdk_loop_prep.main([]) == 0

    written = dict(
        line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
    )
    assert written["needs_agent"] == "false", "a clean PR must not trigger the install"
    assert written["outcome"] == "clean"
    assert written["new_base_sha"] == "a" * 40
