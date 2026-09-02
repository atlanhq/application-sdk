"""Fast track — a re-trigger over a contribution nobody changed.

The case these tests exist for is real and was the reason the check was
written: a PR whose only new commits are merges from base. Its head sha moves,
`sdk-review-reset-on-push.yml` strips the verdict labels and branch protection
dismisses the approval — all correct, all keyed to a sha — while the diff the
review actually read has not changed by a byte. Reviewing it again can only
reach the same verdict more slowly.

So the property under test throughout is: the decision keys on the PR's own
DIFF, not on its head sha, and every path that cannot prove that diff
unchanged declines rather than guesses.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import sdk_loop_prep as prep  # noqa: E402
import sdk_review_approve as approve  # noqa: E402
from sdk_loop_finalize import STOP_TEXT  # noqa: E402
from sdk_loop_prep import (  # noqa: E402
    COMPARE_FILE_CAP,
    OUTCOME_FAST_TRACK,
    contribution_digest,
    fast_track_comment,
    fast_track_decision,
    human_review_after,
)

REVIEWED = "a" * 40
LIVE = "b" * 40


class _Done:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _compare(files: list[dict]) -> str:
    return json.dumps({"files": files})


def _one_file(patch: str) -> str:
    return _compare([{"filename": "a.py", "status": "modified", "patch": patch}])


PATCH = "@@ -1,3 +1,3 @@\n-old\n+new\n context"


# ---------------------------------------------------------------------------
# The digest: patch text, not blob shas
# ---------------------------------------------------------------------------


def test_the_same_contribution_at_two_heads_digests_identically() -> None:
    """The whole feature rests on this. `compare/{base}...{head}` is three-dot,
    so each head is diffed against its OWN merge base — a merge from base moves
    the head and the merge base together and the contribution is untouched."""
    runner = lambda args: _Done(0, _one_file(PATCH))  # noqa: E731
    assert contribution_digest("o/r", "main", REVIEWED, runner=runner) == (
        contribution_digest("o/r", "main", LIVE, runner=runner)
    )


def test_a_changed_hunk_changes_the_digest() -> None:
    """The check must not fast-track real work. If the author pushed a change,
    the patch text differs and the review has to run."""
    before = contribution_digest(
        "o/r", "main", REVIEWED, runner=lambda a: _Done(0, _one_file(PATCH))
    )
    after = contribution_digest(
        "o/r", "main", LIVE, runner=lambda a: _Done(0, _one_file(PATCH + "\n+more"))
    )
    assert before != after


def test_the_digest_is_over_patch_text_not_the_blob_at_head() -> None:
    """Deliberate, and the difference decides the motivating case.

    A blob sha at head is the MERGED result, so a base-side edit anywhere in a
    file this PR also touches would flip it and decline exactly the merge-from-
    base re-trigger this exists for. Patch text moves only when base edits
    inside one of our own hunks. Same patch + different blob sha must still
    digest equal.
    """
    same_patch_new_blob = _compare(
        [{"filename": "a.py", "status": "modified", "patch": PATCH, "sha": "deadbeef"}]
    )
    other_blob = _compare(
        [{"filename": "a.py", "status": "modified", "patch": PATCH, "sha": "cafef00d"}]
    )
    assert contribution_digest(
        "o/r", "main", REVIEWED, runner=lambda a: _Done(0, same_patch_new_blob)
    ) == contribution_digest(
        "o/r", "main", LIVE, runner=lambda a: _Done(0, other_blob)
    ), "a blob sha must not enter the digest while a patch is available"


def test_a_file_with_no_patch_falls_back_to_its_blob() -> None:
    """GitHub omits `patch` for binaries and oversized files. Falling back to
    the blob sha is STRICTER than patch text, not looser: it can only decline a
    fast track, never grant one on a file nobody could compare."""
    binary_a = _compare([{"filename": "logo.png", "status": "modified", "sha": "aaa"}])
    binary_b = _compare([{"filename": "logo.png", "status": "modified", "sha": "bbb"}])
    assert contribution_digest(
        "o/r", "main", REVIEWED, runner=lambda a: _Done(0, binary_a)
    ) != contribution_digest("o/r", "main", LIVE, runner=lambda a: _Done(0, binary_b))


def test_every_unreadable_answer_is_none_and_never_an_empty_digest() -> None:
    """Two empty digests compare EQUAL, which would fast-track a PR nobody
    successfully looked at. So the failure value has to be None, and the
    caller has to treat it as a decline."""
    cases = {
        "gh exited non-zero": _Done(1),
        "unparseable body": _Done(0, "not json"),
        "not an object": _Done(0, "[]"),
        "no files key": _Done(0, "{}"),
        "empty file list": _Done(0, _compare([])),
        "a file entry that is not an object": _Done(0, json.dumps({"files": ["x"]})),
        "no patch and no blob": _Done(
            0, _compare([{"filename": "a.py", "status": "modified"}])
        ),
    }
    for label, done in cases.items():
        assert (
            contribution_digest("o/r", "main", LIVE, runner=lambda a, d=done: d) is None
        ), f"{label} must read as unknown, not as an empty contribution"


def test_a_truncated_compare_declines_rather_than_guesses() -> None:
    """GitHub caps `files` at 300 and paginates past it. A partial list cannot
    prove two contributions identical, so a PR that large is reviewed."""
    big = _compare(
        [
            {"filename": f"f{i}.py", "status": "modified", "patch": "p"}
            for i in range(COMPARE_FILE_CAP)
        ]
    )
    assert (
        contribution_digest("o/r", "main", LIVE, runner=lambda a: _Done(0, big)) is None
    )


# ---------------------------------------------------------------------------
# The human-review guard
# ---------------------------------------------------------------------------


def _reviews(rows: list[tuple[str, str, str]]) -> str:
    return "\n".join("\t".join(r) for r in rows)


def test_any_human_review_after_the_verdict_counts_not_just_changes_requested() -> None:
    """`sdk-review-dismiss-on-human.yml` exists because human review activity
    of ANY kind invalidates a bot approval. A person leaving a COMMENT review
    to raise a concern must not be fast-tracked straight past."""
    rows = _reviews([("2026-09-01T10:00:00Z", "someone", "User")])
    assert (
        human_review_after(
            "o/r", 1, "2026-09-01T09:00:00Z", runner=lambda a: _Done(0, rows)
        )
        is True
    )


def test_bot_reviews_and_atlan_ci_are_not_human_activity() -> None:
    """atlan-ci is a PAT-backed *user*, and it is the identity the approval
    path posts under. Reading its own approval as human activity would make
    the fast track decline every time it had previously succeeded."""
    rows = _reviews(
        [
            ("2026-09-01T10:00:00Z", "atlan-ci", "User"),
            ("2026-09-01T10:01:00Z", "atlan-app-fleet[bot]", "Bot"),
            ("2026-09-01T10:02:00Z", "Copilot", "Bot"),
        ]
    )
    assert (
        human_review_after(
            "o/r", 1, "2026-09-01T09:00:00Z", runner=lambda a: _Done(0, rows)
        )
        is False
    )


def test_a_human_review_before_the_verdict_does_not_count() -> None:
    """The verdict already accounts for anything said before it was posted."""
    rows = _reviews([("2026-08-31T10:00:00Z", "someone", "User")])
    assert (
        human_review_after(
            "o/r", 1, "2026-09-01T09:00:00Z", runner=lambda a: _Done(0, rows)
        )
        is False
    )


def test_an_unreadable_review_listing_is_none_not_no_reviews() -> None:
    """Collapsing "the API did not answer" into "nobody objected" is how a bot
    re-stamps an approval over a human's objection."""
    assert human_review_after("o/r", 1, "x", runner=lambda a: _Done(1)) is None
    malformed = human_review_after(
        "o/r", 1, "x", runner=lambda a: _Done(0, "only-one-field")
    )
    assert malformed is None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def _decide(**over):
    """A decision over a PR whose contribution is unchanged, unless overridden."""
    kwargs = dict(
        repo="o/r",
        pr=1,
        head=LIVE,
        base_ref="main",
        verdict_comment={"created_at": "2026-09-01T09:00:00Z", "body": "x"},
        verdict=approve.READY,
        reviewed_head=REVIEWED,
        ready=approve.READY,
        runner=lambda a: _Done(
            0,
            _one_file(PATCH)
            if "compare" in " ".join(a)
            else _reviews([("2026-08-01T00:00:00Z", "atlan-ci", "User")]),
        ),
    )
    kwargs.update(over)
    return fast_track_decision(**kwargs)


def test_an_unchanged_contribution_fast_tracks() -> None:
    """The motivating case: merges from base only."""
    out = _decide()
    assert out.fires
    assert out.head == LIVE and out.reviewed_head == REVIEWED
    assert "identical" in out.reason


def test_an_unmoved_head_fast_tracks_without_reading_any_diff() -> None:
    """The other half of the case this was built for. When the head has not
    moved at all, the loop is being re-triggered because the approval was LOST
    — an `atlan-ci` rate limit, most often — not because anything changed. The
    verdict still describes the head exactly, so no compare is even needed."""

    def runner(args):
        assert "compare" not in " ".join(args), "an unmoved head needs no diff read"
        return _Done(0, _reviews([]))

    out = _decide(head=REVIEWED, runner=runner)
    assert out.fires
    assert "not moved" in out.reason


def test_needs_fixes_is_never_fast_tracked() -> None:
    """An unchanged diff with findings against it still has those findings.
    Only a READY_TO_MERGE verdict may be carried forward; everything else
    falls through to the normal loop so the resolve phase can act."""
    out = _decide(verdict="NEEDS_FIXES")
    assert not out.fires
    assert "findings still stand" in out.reason


def test_a_changed_diff_is_reviewed() -> None:
    out = _decide(
        runner=lambda a: _Done(
            0,
            _one_file(PATCH if REVIEWED in " ".join(a) else PATCH + "\n+extra")
            if "compare" in " ".join(a)
            else _reviews([]),
        )
    )
    assert not out.fires
    assert "diff has changed" in out.reason


def test_a_human_reviewing_after_the_verdict_blocks_the_fast_track() -> None:
    out = _decide(
        runner=lambda a: _Done(
            0,
            _one_file(PATCH)
            if "compare" in " ".join(a)
            else _reviews([("2026-09-01T10:00:00Z", "a-person", "User")]),
        )
    )
    assert not out.fires
    assert "human" in out.reason


def test_every_missing_precondition_declines() -> None:
    """None of these raise. A fast track that cannot prove itself is simply a
    normal loop run — the behaviour the repo had before the check existed."""
    assert not _decide(verdict_comment=None).fires
    assert not _decide(reviewed_head=None).fires
    assert not _decide(head="").fires
    assert not _decide(verdict_comment={"body": "x"}).fires  # no created_at
    assert not _decide(runner=lambda a: _Done(1)).fires  # nothing readable


# ---------------------------------------------------------------------------
# The comment, which is the entire mechanism
# ---------------------------------------------------------------------------


def test_the_comment_carries_the_markers_the_approval_path_reads() -> None:
    """prep posts no approval of its own. It posts a verdict comment, and
    `sdk-review-approve-on-verdict.yml` approves off it under the one identity
    that is a CODEOWNER. That only happens if the comment speaks the contract."""
    body = fast_track_comment(3539, _decide(), approve.READY, "https://run")
    assert "<!-- SDK_REVIEW -->" in body, "the workflow's `if:` filters on this marker"
    assert approve.extract_verdict(body) == approve.READY
    assert "### Findings" in body, "the loop's summary reads an empty Findings section"
    assert "<!-- SDK_LOOP_FAST_TRACK -->" in body, "a carried verdict must be auditable"


def test_the_comment_stamps_the_LIVE_head_not_the_reviewed_one() -> None:
    """The approval has to land on the sha the PR is on now. Stamping the old
    head would have the approval path either approve a sha nobody is on or
    decide the head had moved past the verdict and skip."""
    body = fast_track_comment(1, _decide(), approve.READY)
    assert approve.extract_reviewed_head(body) == LIVE
    assert (
        f"<!-- FAST_TRACKED_FROM: {REVIEWED} -->" in body
    ), "provenance is still recorded"


def test_the_summary_names_the_outcome() -> None:
    """`_stop_expr()` falls through to 'failed' when no review or resolve ran —
    which is every fast track. Without an entry here the summary tells the
    author a phase broke on the run that did exactly what it should."""
    assert OUTCOME_FAST_TRACK in STOP_TEXT
    assert "failed" not in STOP_TEXT[OUTCOME_FAST_TRACK].lower()


# ---------------------------------------------------------------------------
# Trust: who may author a verdict
# ---------------------------------------------------------------------------


def test_both_reviewer_bots_may_author_a_verdict_and_nobody_else() -> None:
    """@sdk-loop posts its verdicts as the fleet App, not through the mothership
    sandbox. While this set held one login, every consumer that FINDS a verdict
    by listing comments — including `sdk_review_reconcile.py` — was blind to
    every loop verdict, so a loop approval lost to a rate limit could never be
    reconciled.

    The marker alone must never be enough: it is text any PR author can type,
    and the comment it identifies can drive an `atlan-ci` APPROVE.
    """
    marked = {"body": "<!-- SDK_REVIEW -->\n<!-- VERDICT: READY_TO_MERGE -->"}
    for login in ("mothership-ai[bot]", "atlan-app-fleet[bot]"):
        assert approve._is_verdict_comment({**marked, "user": {"login": login}}), login
    assert not approve._is_verdict_comment(
        {**marked, "user": {"login": "a-person"}}
    ), "a forged verdict comment must not be actionable"


# ---------------------------------------------------------------------------
# The wiring, which is the only link that can fail in production alone
# ---------------------------------------------------------------------------


def _drive_prep(monkeypatch, tmp_path, *, fires: bool, posted: bool) -> dict[str, str]:
    """Run the real prep branch of `main()` and return the step outputs it wrote.

    The decision functions above are unit-tested; this covers the wiring
    between them, which nothing else does. `emit_outputs` is a no-op when
    `GITHUB_OUTPUT` is unset, so without a test that sets it, nothing ever
    proved this block writes the value `review-1`'s gate reads. If it did not,
    the gate would see '' and the chain would run — paying for the review AND
    leaving a stray verdict comment, which is worse than not fast-tracking.
    """
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("PHASE", "prep")
    monkeypatch.setenv("ROUND", "0")
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "1")
    monkeypatch.setenv("HEAD_REF", "a-branch")
    monkeypatch.setenv("BASE_SHA", LIVE)

    monkeypatch.setattr(
        prep,
        "pr_state",
        lambda *a, **k: {
            "mergeStateStatus": "CLEAN",
            "headRefOid": LIVE,
            "baseRefName": "main",
        },
    )
    monkeypatch.setattr(prep, "failing_checks", lambda *a, **k: ())
    monkeypatch.setattr(
        prep,
        "fast_track_check",
        lambda *a, **k: type(_decide())(
            fires=fires,
            reason="every commit since came from base",
            head=LIVE,
            reviewed_head=REVIEWED,
        ),
    )
    monkeypatch.setattr(prep, "post_fast_track", lambda *a, **k: posted)

    assert prep.main() == 0, "prep must never fail the run"
    written = {}
    for line in out.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            written[key] = value
    return written


def test_a_fired_fast_track_writes_the_outcome_the_gate_reads(
    monkeypatch, tmp_path
) -> None:
    """`review-1`'s `if:` tests `needs.prep.outputs.outcome != 'fast_track'`.
    This is the write that has to produce that exact string."""
    written = _drive_prep(monkeypatch, tmp_path, fires=True, posted=True)
    assert written.get("outcome") == OUTCOME_FAST_TRACK
    assert (
        written.get("new_base_sha") == LIVE
    ), "the review, if any, runs on the live head"
    assert (
        written.get("pushed_sha") == ""
    ), "prep pushed nothing, so it must claim nothing"
    # `emit_outputs` writes one `key=value` line per value, so a newline in any
    # of them corrupts every output after it.
    assert "\n" not in written.get("detail", "")


def test_a_verdict_comment_that_did_not_post_is_not_reported_as_a_fast_track(
    monkeypatch, tmp_path
) -> None:
    """The comment IS the mechanism — it is what the approval path reads. If it
    did not land there is nothing to approve, so the run must fall through and
    review the PR rather than cancel the chain on a fast track that left no
    trace anywhere."""
    written = _drive_prep(monkeypatch, tmp_path, fires=True, posted=False)
    assert written.get("outcome") != OUTCOME_FAST_TRACK
    assert (
        written.get("outcome") == "clean"
    ), "it falls through to the ordinary prep result"


def test_the_fast_track_lives_in_the_step_that_runs_on_a_clean_pr() -> None:
    """The alignment that its first draft missed.

    `sdk-loop-phase.yml` runs `sdk_loop_prep.py` first and gates the opencode
    install and `sdk_loop_phase.py` on `steps.prep.outputs.needs_agent`. On a
    clean PR that is false, so the deterministic step IS the phase and
    `phase.py`'s prep branch never executes. A fast track placed there passed
    every test and could not fire for the one case it exists for — a clean PR
    whose only new commits are merges from base.

    So: the check lives in `prep.main()`, `phase.py` carries no copy, and a
    fired fast track reports `needs_agent=false` so the workflow skips the
    install rather than paying for an agent that has nothing to do.
    """
    phase_src = pathlib.Path(".github/scripts/sdk_loop_phase.py").read_text(
        encoding="utf-8"
    )
    assert "_fast_track" not in phase_src, "a second copy in phase.py is dead code"
    assert "fast_track_check" not in phase_src
    assert "OUTCOME_FAST_TRACK" not in phase_src

    workflow = pathlib.Path(".github/workflows/sdk-loop-phase.yml").read_text(
        encoding="utf-8"
    )
    assert "run: python3 .github/scripts/sdk_loop_prep.py" in workflow
    assert "steps.prep.outputs.needs_agent == 'true'" in workflow, (
        "the agent phase is gated on needs_agent — that gate is why the fast "
        "track must run in the prep step"
    )
    # The comment links to the run that carried the verdict forward; the step
    # needs the URL to do so.
    prep_step = workflow.split("name: Prep — deterministic pass")[1].split("- name:")[0]
    assert "GHA_RUN_URL" in prep_step


def test_a_fired_fast_track_skips_the_agent_install(monkeypatch, tmp_path) -> None:
    """`needs_agent` gates 18 seconds of npm install and a model call. A fast
    track has nothing for either."""
    written = _drive_prep(monkeypatch, tmp_path, fires=True, posted=True)
    assert written.get("needs_agent") == "false"
