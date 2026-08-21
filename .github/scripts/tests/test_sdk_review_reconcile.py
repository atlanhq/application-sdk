"""Tests for the SDK review approval reconciler.

The regression these pin: a PR whose READY_TO_MERGE verdict still stands but
whose `atlan-ci` approval was lost (rate-limited stamper) must get one posted
without a human re-running a job — and *only* such a PR. Every guard that stops
the reconciler blessing something it should not is asserted here, because the
thing it drives is a CODEOWNER approval on `main`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is absent for a bare module_from_spec.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


approve = _load("sdk_review_approve")
reconcile = _load("sdk_review_reconcile")


REPO = "atlanhq/application-sdk"
PR = 7
HEAD = "a2f276a06384ad38ba3e2e96820a313ed3859db2"
OTHER = "51c160b06a2a350289c7d779f4ab887503f98685"

APP_TOKEN = "app-token"
PAT = "pat-atlan-ci"

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-08-17T11:00:00Z"  # an hour before NOW — past the age gate
RECENT = "2026-08-17T11:59:00Z"  # a minute before NOW — still possibly in flight

RATE_LIMIT_STDERR = "gh: API rate limit exceeded for user ID 62283865. (HTTP 403)"


def ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr
    )


def verdict_body(verdict: str = "READY_TO_MERGE", head: str = HEAD) -> str:
    return (
        "<!-- SDK_REVIEW -->\n"
        f"<!-- VERDICT: {verdict} -->\n"
        f"<!-- REVIEWED_HEAD: {head} -->\n"
        "## SDK Review (mothership)\n"
    )


def comment(
    body: str | None = None,
    created_at: str = OLD,
    login: str = "mothership-ai[bot]",
    comment_id: int = 5,
) -> dict:
    return {
        "id": comment_id,
        "body": verdict_body() if body is None else body,
        "created_at": created_at,
        "user": {"login": login},
    }


def pull(
    number: int = PR,
    head: str = HEAD,
    labels: list[str] | None = None,
) -> dict:
    names = ["sdk-review-approved"] if labels is None else labels
    return {
        "number": number,
        "head": {"sha": head},
        "labels": [{"name": name} for name in names],
    }


def bot_approval() -> dict:
    return {
        "id": 91,
        "state": "APPROVED",
        "user": {"login": "atlan-ci"},
        "body": approve.APPROVAL_SIGNATURE + " READY TO MERGE.",
    }


class FakeGH:
    """Records every `gh` invocation and answers from registered matchers."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.tokens: list[str | None] = []
        self.matchers: list[tuple] = []

    def on(self, predicate, response) -> None:
        """Later registrations override earlier ones, so a test can re-point a
        path that `base_gh()` already stubbed."""
        self.matchers.append((predicate, response))

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        env = kwargs.get("env") or {}
        self.tokens.append(env.get("GH_TOKEN"))
        for predicate, response in reversed(self.matchers):
            if predicate(argv):
                return response() if callable(response) else response
        return ok()

    def called(self, predicate) -> list[list[str]]:
        return [argv for argv in self.calls if predicate(argv)]

    def approver_calls(self) -> list[list[str]]:
        return [argv for argv, token in zip(self.calls, self.tokens) if token == PAT]


def is_pr_list(argv) -> bool:
    # `--paginate` sits before the path here, so match on any positional.
    return argv[1] == "api" and any(
        arg.startswith(f"repos/{REPO}/pulls?state=open") for arg in argv
    )


def is_review_list(argv) -> bool:
    return (
        argv[1] == "api"
        and argv[2] == f"repos/{REPO}/pulls/{PR}/reviews"
        and "--paginate" in argv
    )


def is_approve(argv) -> bool:
    return (
        argv[1] == "api"
        and argv[2] == f"repos/{REPO}/pulls/{PR}/reviews"
        and "POST" in argv
    )


def is_status(argv) -> bool:
    return argv[1] == "api" and argv[2].startswith(f"repos/{REPO}/statuses/")


def is_label_write(argv) -> bool:
    return argv[1] == "api" and f"repos/{REPO}/issues/{PR}/labels" in argv[2]


def is_rate_limit(argv) -> bool:
    return argv[1] == "api" and argv[2] == "rate_limit"


RESET = int(NOW.timestamp()) + 1800  # half an hour out


def base_gh(
    prs: list[dict] | None = None,
    comments: list[dict] | None = None,
    reviews: list[dict] | None = None,
    labels: list[str] | None = None,
    quota_remaining: int = 4999,
    quota_reset: int = RESET,
) -> FakeGH:
    """A repo where PR #7 carries a standing READY verdict and no approval."""
    gh = FakeGH()
    gh.on(is_rate_limit, lambda: ok(f"{quota_remaining}\n{quota_reset}\n"))
    gh.on(
        is_pr_list,
        ok("\n".join(json.dumps(pr) for pr in (prs if prs is not None else [pull()]))),
    )
    # Stateful on purpose: a successful APPROVE has to become visible to the
    # next review listing, because that read-back is how the sweep tells an
    # actual recovery from a stamp its own guards declined.
    gh.on(
        is_review_list,
        lambda: ok(
            json.dumps(
                [(reviews or []) + ([bot_approval()] if gh.called(is_approve) else [])]
            )
        ),
    )
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(json.dumps([comments if comments is not None else [comment()]])),
    )
    # Read back by the stamper itself (fresh head + label snapshot).
    gh.on(lambda a: a[2] == f"repos/{REPO}/pulls/{PR}", ok(HEAD + "\n"))
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}" and "--jq" in a,
        ok("\n".join(["sdk-review-approved"] if labels is None else labels)),
    )
    return gh


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", APP_TOKEN)
    monkeypatch.setenv("APPROVER_TOKEN", PAT)


def run_sweep(gh: FakeGH, **kwargs) -> list:
    """Sweep with a recorded sleeper, so a retry path never really sleeps."""
    slept: list[float] = []
    kwargs.setdefault("sleeper", slept.append)
    outcomes = reconcile.sweep(REPO, runner=gh, now=NOW, **kwargs)
    gh.slept = slept  # type: ignore[attr-defined]
    return outcomes


# --- the recovery this exists for ----------------------------------------


def test_lost_approval_is_reconciled():
    gh = base_gh()
    outcomes = run_sweep(gh)

    assert [(o.number, o.action) for o in outcomes] == [(PR, reconcile.RECONCILED)]
    posted = gh.called(is_approve)
    assert len(posted) == 1
    # Pinned to the reviewed sha, not merely "the head at POST time".
    assert f"commit_id={HEAD}" in posted[0]


def test_reconcile_spends_exactly_one_quota_bearing_atlan_ci_request():
    """The reconciler must not become a new source of the exhaustion it recovers
    from: every read runs on the App token, the PAT only on the APPROVE.

    The pre-flight meter read also carries the PAT, but `GET /rate_limit` does
    not count against the quota it reports — so the budget that matters is
    "one APPROVE", not "one request".
    """
    gh = base_gh()
    run_sweep(gh)

    approver = gh.approver_calls()
    assert [argv for argv in approver if is_approve(argv)] != []
    assert len([argv for argv in approver if is_approve(argv)]) == 1
    assert all(is_approve(argv) or is_rate_limit(argv) for argv in approver)


def test_reconcile_does_not_green_the_sdk_review_status():
    """A green `sdk-review` check with no approving review is the misleading
    state this whole mechanism exists to avoid — the reconciler never writes it."""
    gh = base_gh()
    run_sweep(gh)

    assert gh.called(is_status) == []


# --- guards ---------------------------------------------------------------


def test_pr_without_the_approved_label_is_left_alone():
    """`sdk-review-approved` is the one signal every invalidator clears, so its
    absence means a dismissal, downgrade or push has spoken since the verdict."""
    gh = base_gh(prs=[pull(labels=["needs-triage"])])
    outcomes = run_sweep(gh)

    assert outcomes == []
    assert gh.called(is_approve) == []
    # Not even the review listing is spent on an unlabelled PR.
    assert gh.called(is_review_list) == []


def test_label_stripped_between_the_sweep_and_the_stamp_still_blocks():
    """The sweep's listing can be stale by the time the stamp runs; the stamper
    re-reads the label under REQUIRE_APPROVED_LABEL and refuses."""
    gh = base_gh(labels=[])  # the stamper's own label read comes back empty
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "declined" in outcomes[0].reason
    assert gh.called(is_approve) == []


def test_declining_the_stamp_does_not_resurrect_the_stripped_label():
    """The label is what this cron gates on. If declining to approve re-added
    it, the next tick would read the resurrected label as a lost stamp and
    approve a PR whose verdict an invalidator had deliberately cleared."""
    gh = base_gh(labels=[])
    run_sweep(gh)

    assert gh.called(is_label_write) == []


def test_a_declined_stamp_is_not_reported_as_a_recovery(capsys):
    """Exit 0 from the stamper means "approved OR guarded"; reporting the second
    as a recovery would be the same class of lie this cron exists to catch."""
    gh = base_gh(labels=[])
    reconcile.report(run_sweep(gh), REPO)

    assert "::warning::" not in capsys.readouterr().out


def test_head_moved_past_the_verdict_is_left_alone():
    gh = base_gh(prs=[pull(head=OTHER)])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "head moved" in outcomes[0].reason
    assert gh.called(is_approve) == []


def test_existing_bot_approval_is_a_no_op():
    gh = base_gh(reviews=[bot_approval()])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert outcomes[0].reason == "already approved"
    assert gh.called(is_approve) == []


def test_a_human_approval_does_not_count_as_the_bot_approval():
    """Only an atlan-ci review bearing the bot signature proves the stamp
    landed; a human approval is a different thing entirely."""
    human = {
        "id": 4,
        "state": "APPROVED",
        "user": {"login": "some-engineer"},
        "body": "lgtm",
    }
    gh = base_gh(reviews=[human])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]


def test_non_ready_verdict_is_left_alone():
    gh = base_gh(comments=[comment(body=verdict_body("NEEDS_FIXES"))])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert gh.called(is_approve) == []


def test_forged_verdict_comment_from_another_login_is_ignored():
    """A marker alone is not proof of authorship; only mothership-ai[bot]'s
    verdicts may drive the atlan-ci approval."""
    gh = base_gh(comments=[comment(login="drive-by")])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert outcomes[0].reason == "no verdict comment"
    assert gh.called(is_approve) == []


def test_verdict_without_reviewed_head_is_left_alone():
    gh = base_gh(
        comments=[comment(body="<!-- SDK_REVIEW -->\n### Verdict: READY TO MERGE")]
    )
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert gh.called(is_approve) == []


def test_recent_verdict_is_left_to_the_fast_path():
    """A fast-path run for the same comment may still be retrying; reconciling
    underneath it would risk a duplicate approval."""
    gh = base_gh(comments=[comment(created_at=RECENT)])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert outcomes[0].reason == "verdict too recent to be lost"
    assert gh.called(is_approve) == []


def test_unparseable_comment_timestamp_is_treated_as_too_recent():
    gh = base_gh(comments=[comment(created_at="not-a-date")])
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert gh.called(is_approve) == []


def test_dry_run_reports_without_approving():
    gh = base_gh()
    outcomes = run_sweep(gh, dry_run=True)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "would reconcile" in outcomes[0].reason
    assert gh.called(is_approve) == []


# --- the two regressions from the first live fire -------------------------


def test_a_reconcile_is_reported_even_when_the_listing_has_not_caught_up():
    """GitHub's reviews listing is read-after-write eventually consistent.

    The first live run of this cron approved PR #3232, re-read the listing, saw
    nothing, and reported "the stamper declined" — then printed "Nothing to
    reconcile". A real recovery went unannounced, which is the one thing this
    workflow exists to announce. The stamper now says what it did, so the
    listing lagging cannot rewrite history.
    """
    gh = base_gh()
    # Never shows the approval, however many times it is asked.
    gh.on(is_review_list, ok(json.dumps([[]])))
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
    assert len(gh.called(is_approve)) == 1


def test_an_unreadable_listing_never_approves_blind():
    """Regression from the 2026-08-17 degradation: `_paginated` returned `[]` on
    a 404, which reads as "no approval exists" — the precondition for posting
    one. atlan-ci re-approved the same PR on every tick, silently."""
    gh = base_gh()
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.DEFERRED]
    assert "unreadable" in outcomes[0].reason
    assert gh.called(is_approve) == []


def test_an_unreadable_listing_is_not_reported_as_a_recovery(capsys):
    gh = base_gh()
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))
    reconcile.report(run_sweep(gh), REPO)

    out = capsys.readouterr().out
    assert "had lost" not in out, "a blocked sweep is not a recovery"
    assert "::error::" not in out, "a transient outage is not a human's problem yet"


def test_an_outage_outlasting_a_quota_window_stops_being_a_deferral():
    """Otherwise the reconciler sits in a permanently green skipped loop through
    an outage, saying nothing — the same silence it exists to break."""
    gh = base_gh(comments=[comment(created_at="2026-08-17T09:00:00Z")])
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.FAILED]
    assert "outlasted a full quota window" in outcomes[0].reason
    assert gh.called(is_approve) == []


def test_a_listing_that_breaks_mid_stamp_is_a_failure_not_a_silent_approval():
    """Readable at the prefilter, broken by the time the stamper re-checks."""
    gh = base_gh()
    reads = {"n": 0}

    def listing():
        reads["n"] += 1
        if reads["n"] == 1:
            return ok(json.dumps([[]]))
        return fail("gh: Not Found (HTTP 404)")

    gh.on(is_review_list, listing)
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.FAILED]
    assert gh.called(is_approve) == []


# --- quota pre-flight -----------------------------------------------------


def test_exhausted_quota_spends_no_approve_request_at_all():
    """The original shape discovered exhaustion by taking a 403 — a doomed
    request to learn what a free one already knows. Repeatedly hammering an
    exhausted primary limit is also how it escalates to an abuse block."""
    gh = base_gh(quota_remaining=0)
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.DEFERRED]
    assert gh.called(is_approve) == []
    assert gh.approver_calls() == [
        argv for argv in gh.calls if is_rate_limit(argv)
    ], "the only atlan-ci request should be the free meter read"


def test_deferral_names_the_reset_and_does_not_red_the_run(capsys):
    """Deferring is the self-healing case: the next tick after the reset posts
    it. A red run every ten minutes for an hour would bury the annotation that
    matters when it does NOT clear."""
    gh = base_gh(quota_remaining=0)
    outcomes = run_sweep(gh)
    reconcile.report(outcomes, REPO)

    assert "resets in 30min" in outcomes[0].reason
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "::error::" not in out


def test_main_stays_green_on_a_deferral(monkeypatch):
    monkeypatch.setattr(
        reconcile,
        "sweep",
        lambda *args, **kwargs: [
            reconcile.Outcome(PR, reconcile.DEFERRED, "atlan-ci quota exhausted")
        ],
    )
    assert reconcile.main(["--repo", REPO]) == 0


def test_a_verdict_outliving_a_full_quota_window_reds_the_run():
    """Past one hourly reset, "waiting for quota" stops being an explanation."""
    gh = base_gh(
        quota_remaining=0, comments=[comment(created_at="2026-08-17T09:00:00Z")]
    )
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.FAILED]
    assert "outlasted a full quota window" in outcomes[0].reason
    assert gh.called(is_approve) == []


def test_quota_is_read_once_per_run_not_once_per_pr():
    gh = base_gh(
        quota_remaining=0,
        prs=[pull(number=PR), pull(number=11), pull(number=12)],
    )
    run_sweep(gh)

    assert len(gh.called(is_rate_limit)) == 1


def test_no_candidates_means_no_quota_read():
    """A sweep with nothing to approve must not spend a request establishing
    that it could have."""
    gh = base_gh(reviews=[bot_approval()])
    run_sweep(gh)

    assert gh.called(is_rate_limit) == []


def test_unreadable_quota_still_attempts_the_approval():
    """Failing to read the meter is not evidence the tank is empty."""
    gh = base_gh()
    gh.on(is_rate_limit, fail("network go boom"))
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
    assert len(gh.called(is_approve)) == 1


# --- failure reporting ----------------------------------------------------


def test_quota_emptying_mid_run_is_a_deferral_not_a_failure():
    """Other `atlan-ci` workflows share the quota, so it can empty between the
    pre-flight and the POST. That race is a deferral; re-reading the meter is
    how we tell it from a real failure without parsing stderr."""
    gh = base_gh()
    gh.on(is_approve, fail(RATE_LIMIT_STDERR))
    # Full at pre-flight, empty by the time we ask again.
    reads = {"n": 0}

    def meter():
        reads["n"] += 1
        return ok(f"{0 if reads['n'] > 1 else 4999}\n{RESET}\n")

    gh.on(is_rate_limit, meter)
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.DEFERRED]
    assert gh.called(is_status) == []


def test_a_non_quota_approval_failure_is_a_real_failure():
    gh = base_gh()
    gh.on(is_approve, fail("gh: Validation Failed (HTTP 422)"))
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.FAILED]
    assert outcomes[0].reason == "approval could not be posted"
    assert gh.called(is_status) == []


def test_secondary_throttle_gets_one_inline_retry():
    """Secondary limits clear in seconds, so a single short retry is worth it —
    unlike a primary reset, which the next tick handles instead of this runner."""
    gh = base_gh()
    attempts = {"n": 0}

    def approve_once_then_succeed():
        attempts["n"] += 1
        if attempts["n"] == 1:
            return fail("You have exceeded a secondary rate limit (HTTP 403)")
        return ok()

    gh.on(is_approve, approve_once_then_succeed)
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
    assert len(gh.called(is_approve)) == 2


def test_main_exits_nonzero_when_a_reconcile_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        reconcile,
        "sweep",
        lambda *args, **kwargs: [
            reconcile.Outcome(PR, reconcile.FAILED, "approval could not be posted")
        ],
    )
    assert reconcile.main(["--repo", REPO]) == 1
    assert "::error::" in capsys.readouterr().out


def test_main_exits_zero_and_says_so_when_there_is_nothing_to_do(monkeypatch, capsys):
    monkeypatch.setattr(reconcile, "sweep", lambda *args, **kwargs: [])
    assert reconcile.main(["--repo", REPO]) == 0
    assert "Nothing to reconcile." in capsys.readouterr().out


def test_reconciling_emits_a_warning_annotation(capsys):
    """Silent recovery would hide a worsening rate-limit problem."""
    reconcile.report(
        [reconcile.Outcome(PR, reconcile.RECONCILED, f"approved at {HEAD}")], REPO
    )

    out = capsys.readouterr().out
    assert "::warning::" in out
    assert f"PR #{PR}" in out


def test_reconciling_writes_a_job_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    reconcile.report([reconcile.Outcome(PR, reconcile.RECONCILED, "approved")], REPO)

    written = summary.read_text()
    assert f"https://github.com/{REPO}/pull/{PR}" in written


def test_quiet_sweep_writes_no_job_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    reconcile.report(
        [reconcile.Outcome(PR, reconcile.SKIPPED, "already approved")], REPO
    )

    assert not summary.exists()


# --- environment hygiene --------------------------------------------------


def test_stamper_env_is_restored_after_each_pr(monkeypatch):
    """The stamper reads its inputs from os.environ, so driving it in a loop
    must not leak one PR's settings into the next iteration — or into whatever
    else shares this process."""
    monkeypatch.setenv("WRITE_STATUS", "true")
    monkeypatch.delenv("PR_NUMBER", raising=False)

    with reconcile.stamper_env({"WRITE_STATUS": "false", "PR_NUMBER": "7"}):
        pass

    assert os.environ["WRITE_STATUS"] == "true"
    assert "PR_NUMBER" not in os.environ


def test_sweep_covers_every_labelled_pr():
    gh = base_gh(prs=[pull(number=3, labels=[]), pull(number=PR)])
    outcomes = run_sweep(gh)

    # The unlabelled PR is not reported at all; the labelled one is reconciled.
    assert [(o.number, o.action) for o in outcomes] == [(PR, reconcile.RECONCILED)]


def test_pr_listing_failure_is_loud():
    gh = FakeGH()
    gh.on(is_pr_list, fail("boom"))

    with pytest.raises(SystemExit, match="failed to list open PRs"):
        run_sweep(gh)


# --- min-age plumbing -----------------------------------------------------


def test_min_age_is_configurable():
    gh = base_gh(comments=[comment(created_at=RECENT)])
    outcomes = run_sweep(gh, min_age=timedelta(seconds=30))

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
