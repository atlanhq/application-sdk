"""Prep phase — branch and check hygiene the review cannot do itself."""

from __future__ import annotations

import pathlib
import re
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sdk_loop_prep import (  # noqa: E402
    OUTCOME_CLEAN,
    OUTCOME_CONFLICTS,
    OUTCOME_RED,
    decide,
    needs_agent,
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
