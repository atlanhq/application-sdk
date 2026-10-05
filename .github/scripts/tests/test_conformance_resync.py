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

import base64
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


def test_fork_pr_on_fixed_branch_is_ignored_not_foreign():
    # A fork's same-named branch is not the branch the lane pushes, so it
    # must not stall the repo's resync, and it is never the lane's own PR.
    forked = _pr(5, gate.RESYNC_BRANCH, fork=True)
    keep, dupes, foreign = lane.split_lane_prs([forked])
    assert (keep, dupes, foreign) == (None, [], None)
    ours = _pr(7, gate.RESYNC_BRANCH)
    assert lane.split_lane_prs([forked, ours])[0]["number"] == 7


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
    assert gate.required_checks_green(REPO, "7", runner, echo=False) is True
    runner2 = FakeRunner(default_rc=1)
    assert gate.required_checks_green(REPO, "7", runner2, echo=False) is False


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


def test_no_dispatch_when_the_repo_does_not_auto_merge():
    runner = FakeRunner()
    result: dict = {"trace": []}
    lane._maybe_dispatch(
        REPO,
        _pr(7, gate.RESYNC_BRANCH),
        False,
        runner,
        result,
        repo_automerge=(False, "renovate.json is in soft mode (auto-merge disabled)"),
    )
    assert not any("workflow" in c for c in runner.calls)
    assert (
        result["approvalSkipped"]
        == "renovate.json is in soft mode (auto-merge disabled)"
    )


# ── an unchanged PR must still be approvable to be left alone ────────────

PARENT = "p" * 40
AUTO_JSON = '{"extends": ["github>atlanhq/application-sdk//renovate/fleet"]}'
SOFT_JSON = '{"automerge": false}'


def _parent_runner(pin: str | None, renovate_json: str | None) -> FakeRunner:
    lock = (
        f'[[package]]\nname = "{gate.CONFORMANCE_PACKAGE}"\nversion = "{pin}"\n'
        if pin
        else ""
    )
    answers = {
        (
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{REPO}/contents/uv.lock?ref={PARENT}",
        ): subprocess.CompletedProcess([], 0 if pin else 1, stdout=lock),
        (
            "gh",
            "api",
            f"repos/{REPO}/contents/renovate.json?ref={PARENT}",
            "-q",
            ".content",
        ): subprocess.CompletedProcess(
            [],
            0 if renovate_json else 1,
            stdout=base64.b64encode(renovate_json.encode()).decode()
            if renovate_json
            else "",
        ),
    }
    return FakeRunner(answers, default_rc=1)


def test_unchanged_pr_on_a_bumped_suite_is_re_pushed():
    # Renovate bumped 0.39.0 -> 0.40.0 on main; the templates did not change,
    # so the PR content still matches but its parent pins the old suite.
    ok, why = lane.unchanged_pr_approvable(
        REPO, PARENT, "0.40.0", True, _parent_runner("0.39.0", AUTO_JSON)
    )
    assert not ok and "0.39.0" in why


def test_unchanged_pr_whose_parent_is_soft_is_re_pushed_when_main_auto_merges():
    ok, why = lane.unchanged_pr_approvable(
        REPO, PARENT, "0.39.0", True, _parent_runner("0.39.0", SOFT_JSON)
    )
    assert not ok and "soft" in why


def test_unchanged_pr_left_alone_when_the_parent_still_passes_the_gate():
    assert lane.unchanged_pr_approvable(
        REPO, PARENT, "0.39.0", True, _parent_runner("0.39.0", AUTO_JSON)
    ) == (True, "")


def test_soft_repo_does_not_require_an_auto_parent():
    # No approval is dispatched in a soft repo, so the mode must not force a
    # re-push on every run.
    assert lane.unchanged_pr_approvable(
        REPO, PARENT, "0.39.0", False, _parent_runner("0.39.0", SOFT_JSON)
    ) == (True, "")


def test_read_clone_file_reads_the_checkout_and_refuses_symlinks(tmp_path):
    (tmp_path / "uv.lock").write_text("lock")
    (tmp_path / "renovate.json").symlink_to(tmp_path / "uv.lock")
    assert lane.read_clone_file(str(tmp_path), "uv.lock") == "lock"
    assert lane.read_clone_file(str(tmp_path), "renovate.json") is None
    assert lane.read_clone_file(str(tmp_path), "missing") is None


# ── resolved-at reuse never trusts an edited body ────────────────────────


def test_resolved_at_from_an_edited_body_is_not_reused():
    now = "2026-10-01T00:00:00Z"
    for edited in ("2099-01-01T00:00:00Z", "2026-13-45T00:00:00Z"):
        keep = {"body": gate.pr_marker("0.39.0", edited)}
        assert lane.choose_resolved_at(keep, "0.39.0", now) == now


# ── a failed render disarms the PR it can no longer reproduce ────────────


def _lane_runner(keep: dict) -> FakeRunner:
    return FakeRunner(
        {
            (
                "gh",
                "api",
                f"repos/{REPO}/pulls?state=open&per_page=100",
                "--paginate",
                "--slurp",
            ): subprocess.CompletedProcess([], 0, stdout=json.dumps([[keep]])),
        }
    )


def _run_failing_render(monkeypatch, dry_run: bool) -> tuple[dict, FakeRunner]:
    lock = f'[[package]]\nname = "{gate.CONFORMANCE_PACKAGE}"\nversion = "0.39.0"\n'
    monkeypatch.setattr(
        lane,
        "read_clone_file",
        lambda work, path: lock if path == "uv.lock" else '{"extends": []}',
    )
    monkeypatch.setattr(lane, "run_bootstrap", lambda *a: (1, "", "index outage"))
    runner = _lane_runner(_pr(7, gate.RESYNC_BRANCH))
    result = lane.process_repo(
        REPO,
        identity=(BOT, "bot@example.invalid"),
        resolved_now=AT,
        dry_run=dry_run,
        automerge_enabled=True,
        diffs_dir=None,
        runner=runner,
    )
    return result, runner


def test_failed_render_disarms_the_open_lane_pr(monkeypatch):
    result, runner = _run_failing_render(monkeypatch, dry_run=False)
    assert result["action"] == "error"
    assert ["gh", "pr", "merge", "7", "--repo", REPO, "--disable-auto"] in runner.calls
    assert not any(c[:3] == ["gh", "pr", "close"] for c in runner.calls)


def test_failed_render_in_a_dry_run_changes_nothing(monkeypatch):
    result, runner = _run_failing_render(monkeypatch, dry_run=True)
    assert result["automerge"] == "would disarm"
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in runner.calls)
