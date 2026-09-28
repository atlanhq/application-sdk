"""Tests for .github/scripts/conformance_resync.py — the conformance resync
lane (FND-2848/FND-2868).

Focuses on the pure decision logic: eligibility, the auto-merge gate, one-PR-
per-repo bookkeeping, PR text, and the new approval-dispatch rule. The
byte-identical re-render is the approval gate's job
(test_resync_approval_conditions.py), not the lane's — this lane only needs to
stage/commit the way that gate expects, which is why it imports
``stage_like_the_lane`` rather than re-implementing it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conformance_resync as lane  # noqa: E402
import resync_approval_conditions as gate  # noqa: E402

REPO = "atlanhq/atlan-example-app"
AT = "2026-09-28T12:00:00Z"
BOT = gate.RESYNC_AUTHOR


def _pr(
    number: int,
    ref: str,
    *,
    author: str = BOT,
    fork: bool = False,
    state: str = "open",
    sha: str = "h" * 40,
) -> dict:
    return {
        "number": number,
        "state": state,
        "user": {"login": author},
        "head": {
            "ref": ref,
            "sha": sha,
            "repo": {"full_name": "someone/fork" if fork else REPO},
        },
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }


class FakeRunner:
    """Answers a fixed table of gh/git calls; records every call made."""

    def __init__(
        self,
        answers: dict[tuple, subprocess.CompletedProcess] | None = None,
        default_rc: int = 0,
    ):
        self.answers = answers or {}
        self.default_rc = default_rc
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        key = tuple(args)
        if key in self.answers:
            return self.answers[key]
        return subprocess.CompletedProcess(args, self.default_rc, stdout="", stderr="")


# ── eligibility ───────────────────────────────────────────────────────────


def test_eligible_when_pinned():
    assert lane.resync_eligibility("0.39.0") == (True, "")


def test_ineligible_when_no_pin():
    ok, why = lane.resync_eligibility(None)
    assert not ok
    assert "uv.lock" in why


# ── auto-merge gate ───────────────────────────────────────────────────────


def test_automerge_auto_mode():
    cfg = '{"extends": ["github>atlanhq/application-sdk//renovate/fleet"]}'
    assert lane.automerge_allowed(cfg) == (True, "")


def test_automerge_soft_mode_blocks():
    cfg = '{"automerge": false}'
    ok, why = lane.automerge_allowed(cfg)
    assert not ok and "soft" in why.lower()


def test_automerge_scoped_rule_does_not_block():
    cfg = '{"packageRules": [{"matchPackageNames": ["pydantic"], "automerge": false}]}'
    assert lane.automerge_allowed(cfg)[0]


def test_automerge_fails_closed_when_unreadable():
    assert not lane.automerge_allowed(None)[0]
    assert not lane.automerge_allowed("{not json")[0]


# ── one PR per repo ───────────────────────────────────────────────────────


def test_split_keeps_fixed_branch():
    prs = [
        _pr(1, "renovate/lock-file-maintenance", author="renovate[bot]"),
        _pr(2, gate.RESYNC_BRANCH),
    ]
    keep, dupes, foreign = lane.split_lane_prs(prs)
    assert keep["number"] == 2
    assert dupes == [] and foreign is None


def test_split_flags_duplicate_lane_prs():
    prs = [_pr(2, gate.RESYNC_BRANCH), _pr(3, gate.RESYNC_BRANCH + "-old")]
    keep, dupes, _foreign = lane.split_lane_prs(prs)
    assert keep["number"] == 2
    assert [d["number"] for d in dupes] == [3]


def test_foreign_pr_on_fixed_branch_is_detected_and_never_merged_with_keep():
    human = _pr(9, gate.RESYNC_BRANCH, author="some-engineer")
    keep, dupes, foreign = lane.split_lane_prs([human])
    assert keep is None and dupes == []
    assert foreign["number"] == 9


def test_fork_pr_on_fixed_branch_is_foreign_not_keep():
    forked = _pr(5, gate.RESYNC_BRANCH, fork=True)
    keep, _dupes, foreign = lane.split_lane_prs([forked])
    assert keep is None
    assert foreign["number"] == 5


# ── PR text ───────────────────────────────────────────────────────────────


def test_marker_round_trip_and_body_leads_with_marker():
    body = lane.render_pr_body(
        suite_version="0.39.0",
        resolved_at=AT,
        touched=["renovate.json"],
        lost={},
        automerge=True,
        automerge_reason="",
    )
    assert body.startswith(gate.pr_marker("0.39.0", AT))
    assert gate.marker_resolved_at(body) == AT
    assert gate.marker_suite_version(body) == "0.39.0"
    assert "armed" in body


def test_body_flags_lost_settings_and_holds_automerge():
    body = lane.render_pr_body(
        suite_version="0.39.0",
        resolved_at=AT,
        touched=[".github/workflows/tests.yaml"],
        lost={".github/workflows/tests.yaml": ["unit: 95"]},
        automerge=False,
        automerge_reason="settings would be lost",
    )
    assert "unit: 95" in body
    assert "not armed" in body


def test_body_is_stable_across_runs():
    kwargs = dict(
        suite_version="0.39.0",
        resolved_at=AT,
        touched=["renovate.json"],
        lost={},
        automerge=True,
        automerge_reason="",
    )
    assert lane.render_pr_body(**kwargs) == lane.render_pr_body(**kwargs)
    assert "actions/runs" not in lane.render_pr_body(**kwargs)


def _commits_runner(commits: list[dict]) -> FakeRunner:
    return FakeRunner(
        {
            (
                "gh",
                "api",
                f"repos/{REPO}/pulls/7/commits",
                "--paginate",
                "--slurp",
            ): subprocess.CompletedProcess([], 0, stdout=json.dumps([commits])),
        }
    )


def _commit(sha: str, parents: int = 1, author: str = BOT) -> dict:
    return {
        "sha": sha,
        "author": {"login": author},
        "parents": [{"sha": f"p{i}" * 20} for i in range(parents)],
    }


def test_lane_commits_ok_for_the_single_lane_commit():
    head = "h" * 40
    assert lane.lane_commits_ok(REPO, 7, head, _commits_runner([_commit(head)])) is True


def test_lane_commits_not_ok_after_update_branch_merge():
    head = "h" * 40
    commits = [_commit("a" * 40), _commit(head, parents=2)]
    assert lane.lane_commits_ok(REPO, 7, head, _commits_runner(commits)) is False


def test_lane_commits_not_ok_for_foreign_commit():
    head = "h" * 40
    commits = [_commit(head, author="someone")]
    assert lane.lane_commits_ok(REPO, 7, head, _commits_runner(commits)) is False


def test_pr_title_names_the_pinned_version():
    assert "0.39.0" in lane.pr_title("0.39.0")


# ── approval dispatch (FND-2868) ─────────────────────────────────────────


def _approved_review(head_sha: str, *, signature: bool = True) -> dict:
    return {
        "user": {"login": gate.APPROVER_LOGIN},
        "state": "APPROVED",
        "commit_id": head_sha,
        "body": gate.RESYNC_SIGNATURE + " ..." if signature else "looks fine",
    }


def test_dispatch_when_checks_green_and_unapproved():
    pr = _pr(7, gate.RESYNC_BRANCH, sha="a" * 40)
    assert lane.should_dispatch_approval(pr, True, []) is True


def test_no_dispatch_when_checks_not_green():
    pr = _pr(7, gate.RESYNC_BRANCH, sha="a" * 40)
    assert lane.should_dispatch_approval(pr, False, []) is False


def test_no_dispatch_when_already_approved_with_signature_on_this_head():
    pr = _pr(7, gate.RESYNC_BRANCH, sha="a" * 40)
    assert (
        lane.should_dispatch_approval(pr, True, [_approved_review("a" * 40)]) is False
    )


def test_dispatch_when_approval_is_on_a_stale_head():
    pr = _pr(7, gate.RESYNC_BRANCH, sha="a" * 40)
    assert lane.should_dispatch_approval(pr, True, [_approved_review("b" * 40)]) is True


def test_dispatch_ignores_approvals_without_the_signature():
    pr = _pr(7, gate.RESYNC_BRANCH, sha="a" * 40)
    stray = _approved_review("a" * 40, signature=False)
    assert lane.should_dispatch_approval(pr, True, [stray]) is True


def test_no_dispatch_when_pr_closed():
    pr = _pr(7, gate.RESYNC_BRANCH, sha="a" * 40, state="closed")
    assert lane.should_dispatch_approval(pr, True, []) is False


def test_no_dispatch_when_no_pr():
    assert lane.should_dispatch_approval(None, True, []) is False


def test_checks_all_green_reads_required_checks_exit_code():
    runner = FakeRunner(
        {
            (
                "gh",
                "pr",
                "checks",
                "7",
                "--repo",
                REPO,
                "--required",
            ): subprocess.CompletedProcess([], 0),
        }
    )
    assert lane.checks_all_green(REPO, 7, runner) is True
    runner2 = FakeRunner(default_rc=1)
    assert lane.checks_all_green(REPO, 7, runner2) is False


def test_dispatch_approval_invokes_the_repos_own_approver():
    runner = FakeRunner()
    lane.dispatch_approval(REPO, 7, runner)
    assert runner.calls[-1] == [
        "gh",
        "workflow",
        "run",
        lane.APPROVE_WORKFLOW,
        "-R",
        REPO,
        "-f",
        "pr_number=7",
    ]


# ── shared contract with the approval gate ───────────────────────────────


def test_lane_and_gate_agree_on_identity_constants():
    # If these ever drift, the lane opens PRs the gate is not configured to
    # trust at all.
    assert lane.gate.RESYNC_AUTHOR == gate.RESYNC_AUTHOR
    assert lane.gate.RESYNC_BRANCH == gate.RESYNC_BRANCH
    assert lane.gate.CONFORMANCE_PACKAGE == gate.CONFORMANCE_PACKAGE


def test_resolved_at_reused_for_same_suite_and_renewed_on_bump():
    keep = {"body": gate.pr_marker("0.39.0", AT)}
    assert lane.choose_resolved_at(keep, "0.39.0", "2026-10-01T00:00:00Z") == AT
    assert (
        lane.choose_resolved_at(keep, "0.40.0", "2026-10-01T00:00:00Z")
        == "2026-10-01T00:00:00Z"
    )
    assert lane.choose_resolved_at(None, "0.39.0", AT) == AT


def _files_runner(paths: list[str], blobs: dict[str, tuple[str, str]]) -> FakeRunner:
    answers = {
        (
            "gh",
            "api",
            f"repos/{REPO}/pulls/7/files",
            "--paginate",
            "--slurp",
        ): subprocess.CompletedProcess(
            [], 0, stdout=json.dumps([[{"filename": p} for p in paths]])
        ),
    }
    for path, (ours, theirs) in blobs.items():
        answers[("git", "rev-parse", "--verify", "-q", f"HEAD:{path}")] = (
            subprocess.CompletedProcess([], 0, stdout=ours + "\n")
        )
        answers[("git", "rev-parse", "--verify", "-q", f"FETCH_HEAD:{path}")] = (
            subprocess.CompletedProcess([], 0, stdout=theirs + "\n")
        )
    return FakeRunner(answers)


def test_pr_matches_render_per_path_ignores_unrelated_main_changes():
    runner = _files_runner(["renovate.json"], {"renovate.json": ("b1", "b1")})
    assert lane.pr_matches_render(REPO, 7, ["renovate.json"], "/w", runner) is True


def test_pr_matches_render_detects_changed_content_or_paths():
    runner = _files_runner(["renovate.json"], {"renovate.json": ("b1", "b2")})
    assert lane.pr_matches_render(REPO, 7, ["renovate.json"], "/w", runner) is False
    runner = _files_runner(["renovate.json", "x.py"], {"renovate.json": ("b1", "b1")})
    assert lane.pr_matches_render(REPO, 7, ["renovate.json"], "/w", runner) is False


def test_withdraw_closes_the_lane_pr_and_never_dispatches():
    runner = FakeRunner()
    result: dict = {"trace": []}
    lane.withdraw_lane_pr(
        REPO, _pr(7, gate.RESYNC_BRANCH), "held", False, runner, result
    )
    assert result["closed"] == 7
    assert not any("workflow" in c for c in runner.calls)


def test_dispatch_failure_is_reported_not_raised():
    assert lane.dispatch_approval(REPO, 7, FakeRunner(default_rc=1)) == "gh exited 1"
    assert lane.dispatch_approval(REPO, 7, FakeRunner()) == ""


def test_pr_matches_render_when_both_sides_delete_the_path():
    runner = _files_runner(["retired.sh"], {"retired.sh": ("", "")})
    assert lane.pr_matches_render(REPO, 7, ["retired.sh"], "/w", runner) is True


def test_pr_matches_render_detects_a_delete_on_one_side_only():
    runner = _files_runner(["retired.sh"], {"retired.sh": ("", "b1")})
    assert lane.pr_matches_render(REPO, 7, ["retired.sh"], "/w", runner) is False
