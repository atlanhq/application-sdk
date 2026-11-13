"""Tests for .github/scripts/vuln_auto_merge_gate.py — the auto-merge boundary."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import vuln_auto_merge_gate as gate

TRUSTED = {"atlan-ci", "atlan-app-fleet[bot]"}
LABEL = "vuln-auto-merge"


# ---------------------------------------------------------------------------
# classify_shape — the path allowlist
# ---------------------------------------------------------------------------


def test_allowlist_only_is_allowlist_shape():
    assert classify(".security/base-allowlist.json") == "allowlist"


def classify(*files):
    return gate.classify_shape(list(files))


def test_bump_subset_is_bump_shape():
    assert classify("uv.lock") == "bump"
    assert classify("pyproject.toml") == "bump"
    assert classify("pyproject.toml", "uv.lock") == "bump"
    assert classify("pyproject.toml", "uv.lock", "packages/conformance/uv.lock") == (
        "bump"
    )


def test_only_the_conformance_lock_rides_the_bump_shape():
    # Full paths: another package's lock or pyproject is not a vuln bump.
    assert classify("uv.lock", "packages/api/uv.lock") is None
    assert classify("uv.lock", "packages/conformance/pyproject.toml") is None


def test_empty_is_none():
    assert classify() is None
    assert gate.classify_shape([""]) is None


def test_allowlist_plus_extra_file_is_none():
    # An allowlist PR that also touches anything else must NOT match.
    assert classify(".security/base-allowlist.json", "README.md") is None


def test_bump_plus_source_file_is_none():
    # Plan verification (c): a bump that also edits a .py source file is refused.
    assert classify("uv.lock", "application_sdk/foo.py") is None
    assert classify("pyproject.toml", "Dockerfile") is None


def test_mixed_allowlist_and_bump_is_none():
    # The two shapes are strictly separate.
    assert classify(".security/base-allowlist.json", "uv.lock") is None


def test_same_named_file_in_subdir_is_none():
    # Only root-level manifests count; a nested pyproject.toml must not ride.
    assert classify("packages/conformance/pyproject.toml") is None
    assert classify("subdir/uv.lock") is None


# ---------------------------------------------------------------------------
# evaluate — full gate
# ---------------------------------------------------------------------------


def _eval(**overrides):
    base = dict(
        author="atlan-ci",
        state="open",
        draft=False,
        head_sha="abc",
        eval_sha="abc",
        labels=[LABEL],
        filenames=[".security/base-allowlist.json"],
        label_name=LABEL,
        trusted_authors=TRUSTED,
    )
    base.update(overrides)
    return gate.evaluate(**base)


def test_evaluate_happy_path_allowlist():
    ok, _reason, shape = _eval()
    assert ok and shape == "allowlist"


def test_evaluate_happy_path_bump():
    ok, _reason, shape = _eval(filenames=["pyproject.toml", "uv.lock"])
    assert ok and shape == "bump"


def test_evaluate_untrusted_author_rejected():
    ok, _r, _s = _eval(author="random-user")
    assert not ok


def test_evaluate_fleet_bot_trusted():
    ok, _r, _s = _eval(author="atlan-app-fleet[bot]")
    assert ok


def test_retired_rover_identity_is_not_trusted_by_default():
    # The mothership rover is retired; its App still acts for sdk-review, so it
    # must not be able to put a PR on the auto-merge path.
    assert "mothership-ai[bot]" not in gate.DEFAULT_AUTHORS.split(",")


def test_evaluate_draft_rejected():
    assert not _eval(draft=True)[0]


def test_evaluate_closed_rejected():
    assert not _eval(state="closed")[0]


def test_evaluate_head_moved_rejected():
    # Plan verification: race guard.
    assert not _eval(head_sha="def", eval_sha="abc")[0]


def test_evaluate_missing_label_rejected():
    # Plan verification (d): no label → refused.
    assert not _eval(labels=[])[0]


def test_evaluate_non_matching_files_rejected():
    assert not _eval(filenames=["application_sdk/foo.py"])[0]


# ---------------------------------------------------------------------------
# process_pr — wired with a fake gh runner
# ---------------------------------------------------------------------------


Review = dict[str, Any]

APPROVAL: Review = {
    "user": {"login": "atlan-ci"},
    "state": "APPROVED",
    "body": f"{gate.APPROVAL_SIGNATURE} allowlist PR from the vuln triage.",
}
COMMENT: Review = {"user": {"login": "someone"}, "state": "COMMENTED", "body": "lgtm"}


class _FakeRunner:
    """Minimal gh stub: returns canned JSON per api call, records actions.

    ``review_pages`` is the reviews listing as the API pages it (30 per page).
    The stub answers the two ways gh can read it: ``--paginate --slurp`` gets one
    JSON array of page arrays, while ``--paginate --jq`` runs the filter per page
    — the per-page behaviour that made ``| length`` print ``"1\n0"``.
    """

    def __init__(
        self,
        meta: dict[str, Any],
        files: list[str],
        approved_count: int = 0,
        *,
        review_pages: list[list[Review]] | None = None,
        reviews_fail: bool = False,
        commit_prs: list[dict[str, Any]] | None = None,
        commit_prs_fail: bool = False,
    ) -> None:
        self.meta = meta
        self.files = files
        self.review_pages: list[list[Review]] = (
            review_pages if review_pages is not None else [[APPROVAL] * approved_count]
        )
        self.reviews_fail = reviews_fail
        self.commit_prs: list[dict[str, Any]] = commit_prs or []
        self.commit_prs_fail = commit_prs_fail
        self.reviews: list[list[str]] = []
        self.merges: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        out = ""
        rc = 0
        if "/files" in joined:
            out = "\n".join(self.files)
        elif "/reviews" in joined:
            if self.reviews_fail:
                rc, out = 1, '{"message": "Server Error"}'
            elif "--slurp" in cmd:
                out = json.dumps(self.review_pages)
            else:  # --jq '[… select …] | length', applied page by page
                out = "\n".join(
                    str(
                        sum(
                            1
                            for r in page
                            if r["user"]["login"] == "atlan-ci"
                            and r["state"] == "APPROVED"
                            and str(r["body"]).startswith(gate.APPROVAL_SIGNATURE)
                        )
                    )
                    for page in self.review_pages
                )
        elif "/commits/" in joined:
            if self.commit_prs_fail:
                rc, out = 1, '{"message": "Server Error"}'
            else:
                out = json.dumps(self.commit_prs)
        elif "/pulls/" in joined and "--jq" in joined:
            out = json.dumps(self.meta)
        elif cmd[:3] == ["gh", "pr", "review"]:
            self.reviews.append(cmd)
        elif cmd[:3] == ["gh", "pr", "merge"]:
            self.merges.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=rc, stdout=out, stderr=""
        )


APPROVER = "atlan-ci"


def _meta(author="atlan-app-fleet[bot]", **kw):
    base = {
        "author": author,
        "state": "open",
        "draft": False,
        "head_sha": "abc",
        "labels": [LABEL],
    }
    base.update(kw)
    return base


def test_process_pr_approves_and_merges_rover_pr():
    # Triage PR (atlan-app-fleet[bot]) → atlan-ci approves + auto-merges.
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"])
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is True
    assert len(r.reviews) == 1
    assert len(r.merges) == 1
    assert "--auto" in r.merges[0]


def test_process_pr_self_authored_skips_approval():
    # Legacy shape: removal PR authored by atlan-ci (the approver) → NO
    # self-approval (would 422). Auto-merge is still armed, but without an
    # approval the PR cannot enter the merge queue — a human must approve.
    r = _FakeRunner(_meta(author="atlan-ci"), [".security/base-allowlist.json"])
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    assert r.reviews == []  # never self-approve
    assert len(r.merges) == 1


def test_process_pr_approves_fleet_app_authored_removal_pr():
    # Regression: the reconcile removal PR is authored by atlan-app-fleet[bot]
    # precisely so atlan-ci CAN approve it. Author != approver, so the normal
    # approve + queue path must run — this is what stops these PRs stalling at
    # REVIEW_REQUIRED and needing a human click.
    r = _FakeRunner(
        _meta(author="atlan-app-fleet[bot]"), [".security/base-allowlist.json"]
    )
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is True
    assert len(r.reviews) == 1
    assert len(r.merges) == 1


def test_fleet_app_bot_is_a_default_trusted_author():
    # The workflow relies on the shipped default, not on the test's TRUSTED set.
    assert "atlan-app-fleet[bot]" in gate.DEFAULT_AUTHORS.split(",")


def test_enable_automerge_passes_no_merge_method_flag():
    # Regression: `main` has a merge queue, which owns the merge strategy. Passing
    # --squash makes gh refuse ("The merge strategy for main is set by the merge
    # queue") and auto-merge is silently never enabled, so nothing ever queues.
    calls = []

    def runner(cmd, check=False, capture_output=False, text=False):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    gate.enable_automerge("o/r", "5", runner)
    assert calls == [["gh", "pr", "merge", "5", "--repo", "o/r", "--auto"]]
    for flag in ("--squash", "--merge", "--rebase"):
        assert flag not in calls[0]


def test_process_pr_skips_when_files_unsafe():
    r = _FakeRunner(_meta(), ["uv.lock", "application_sdk/foo.py"])
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    assert r.reviews == []
    assert r.merges == []


def test_process_pr_idempotent_when_already_approved():
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], approved_count=1)
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    # No new approval, but auto-merge is (re-)ensured.
    assert r.reviews == []
    assert len(r.merges) == 1


def test_process_pr_idempotent_when_approval_is_on_an_earlier_review_page():
    # Regression (FND-4898): with --paginate, --jq ran per page, so a PR whose
    # signature approval sat on page 1 of 2 printed "1\n0"; int() raised, the
    # gate read "not approved" and posted a duplicate approval.
    pages = [[APPROVAL] + [COMMENT] * 29, [COMMENT] * 3]
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], review_pages=pages)
    acted = gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert acted is False
    assert r.reviews == []
    assert len(r.merges) == 1


def test_process_pr_approves_when_no_page_holds_a_live_approval():
    dismissed = {**APPROVAL, "state": "DISMISSED"}
    pages = [[COMMENT] * 30, [dismissed]]
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], review_pages=pages)
    assert gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r) is True
    assert len(r.reviews) == 1


def test_already_approved_honours_the_configured_approver():
    # VULN_AUTOMERGE_APPROVER is configurable; the idempotency check must look
    # for that identity's approval, not a hard-coded atlan-ci.
    r = _FakeRunner(_meta(), [], approved_count=1)
    assert gate.already_approved("o/r", "5", "atlan-ci", r) is True
    assert gate.already_approved("o/r", "5", "other-ci", r) is False


def test_unreadable_reviews_raise_instead_of_approving():
    # Previously a failed listing read as "not approved" and posted an approval.
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], reviews_fail=True)
    with pytest.raises(gate.GhError):
        gate.process_pr("o/r", "5", "abc", LABEL, TRUSTED, APPROVER, r)
    assert r.reviews == []
    assert r.merges == []


def _env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for key in ("EVENT_NAME", "RUN_SHA", "DISPATCH_PR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPO", "o/r")
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_main_evaluates_open_prs_for_the_run_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, EVENT_NAME="workflow_run", RUN_SHA="abc")
    r = _FakeRunner(
        _meta(),
        [".security/base-allowlist.json"],
        commit_prs=[
            {"number": 5, "state": "open"},
            {"number": 6, "state": "closed"},
        ],
    )
    assert gate.main(r) == 0
    assert [c[3] for c in r.reviews] == ["5"]


def test_main_fails_the_step_when_a_pr_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, EVENT_NAME="workflow_run", RUN_SHA="abc")
    r = _FakeRunner(
        _meta(),
        [".security/base-allowlist.json"],
        reviews_fail=True,
        commit_prs=[{"number": 5, "state": "open"}],
    )
    assert gate.main(r) == 1
    assert r.reviews == []


def test_main_fails_the_step_when_the_pr_listing_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unread commit->PRs listing is not "no open PRs": the step must go red
    # rather than exit 0 having evaluated nothing.
    _env(monkeypatch, EVENT_NAME="workflow_run", RUN_SHA="abc")
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], commit_prs_fail=True)
    assert gate.main(r) == 1
    assert r.reviews == []
    assert r.merges == []


def test_main_exits_clean_when_no_open_pr_heads_the_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, EVENT_NAME="workflow_run", RUN_SHA="abc")
    r = _FakeRunner(_meta(), [".security/base-allowlist.json"], commit_prs=[])
    assert gate.main(r) == 0
    assert r.reviews == []
