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
reconcile = _load("review_approval_reconcile")


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


# === lens ==================================================================
#
# The same regression for the second source: lens's last step posts its
# `atlan-ci` APPROVE and, when that one POST fails, only warns. These pin that a
# standing lens verdict gets its approval back, and that every way a lens
# approval is invalidated keeps the reconciler from replaying it.

from lens import approve as lens_approve  # noqa: E402
from lens.findings import Finding, PRState  # noqa: E402
from lens.github import GitHub as LensGitHub  # noqa: E402
from lens.review import SUMMARY_MARKER  # noqa: E402

LENS_BOT = "atlan-app-fleet[bot]"


def lens_state(**overrides) -> PRState:
    fields = {
        "reviewed_head": HEAD,
        "round": 3,
        "history": [{"round": 3, "head": HEAD[:12], "incomplete": False}],
    }
    fields.update(overrides)
    return PRState(**fields)


def lens_summary(state: PRState | None = None, login: str = LENS_BOT) -> dict:
    return {
        "id": 11,
        "body": f"{SUMMARY_MARKER}\n## lens\nReady to merge\n"
        + (state or lens_state()).encode(),
        "created_at": OLD,
        "user": {"login": login},
    }


def lens_status(
    state: str = "success", created_at: str = OLD, creator: str = LENS_BOT
) -> dict:
    return {
        "context": "lens",
        "state": state,
        "created_at": created_at,
        "creator": {"login": creator},
    }


def lens_review(state: str = "APPROVED", head: str = HEAD, review_id: int = 51) -> dict:
    return {
        "id": review_id,
        "state": state,
        "commit_id": head,
        "user": {"login": "atlan-ci"},
        "body": lens_approve.SIGNATURE + " — every finding at every level is resolved.",
    }


class FakeLensAPI:
    """The REST API behind lens's own client, answering per token.

    Each `LensGitHub` gets a transport bound to its token, so a test can prove
    the `atlan-ci` PAT is spent on the APPROVE and nothing else.
    """

    def __init__(
        self,
        *,
        statuses: list[dict] | None = None,
        comments: list[dict] | None = None,
        reviews: list[dict] | None = None,
        pr: dict | None = None,
    ) -> None:
        self.statuses = [lens_status()] if statuses is None else statuses
        self.comments = [lens_summary()] if comments is None else comments
        self.reviews = list(reviews or [])
        self.pr = pr or {
            "number": PR,
            "state": "open",
            "draft": False,
            "head": {"sha": HEAD},
            "user": {"login": "a-contributor"},
        }
        self.approve_response: tuple[int, str] = (200, "{}")
        self.fail: dict[str, tuple[int, str]] = {}
        self.calls: list[tuple[str, str, str]] = []
        # Runs when the approve step reads the PR: a hook for a change that
        # lands between that read and the POST.
        self.on_pr_read = None

    def transport(self, token: str):
        def call(method, path, body=None, accept=""):
            self.calls.append((token, method, path))
            for prefix, response in self.fail.items():
                if path.startswith(prefix):
                    return response
            base = f"/repos/{REPO}"
            if method == "GET" and path.startswith(f"{base}/commits/{HEAD}/statuses"):
                # Paginated like GitHub: 100 per page, newest first.
                page = int(path.rsplit("page=", 1)[1]) if "&page=" in path else 1
                return 200, json.dumps(self.statuses[(page - 1) * 100 : page * 100])
            if method == "GET" and path.startswith(f"{base}/issues/{PR}/comments"):
                return 200, json.dumps(self.comments)
            if method == "GET" and path.startswith(f"{base}/pulls/{PR}/reviews"):
                return 200, json.dumps(self.reviews)
            if method == "GET" and path == f"{base}/pulls/{PR}":
                if self.on_pr_read is not None:
                    self.on_pr_read()
                return 200, json.dumps(self.pr)
            if method == "POST" and path == f"{base}/pulls/{PR}/reviews":
                status, text = self.approve_response
                if status < 300:
                    self.reviews.append(lens_review(review_id=99))
                return status, text
            raise AssertionError(f"unexpected lens call: {method} {path}")

        return call

    def source(self):
        return reconcile.LensSource(
            LensGitHub(REPO, token=APP_TOKEN, transport=self.transport(APP_TOKEN)),
            LensGitHub(REPO, token=PAT, transport=self.transport(PAT)),
        )

    def approvals(self) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[1] == "POST"]

    def pat_calls(self) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[0] == PAT]


def is_graphql(argv) -> bool:
    return argv[1] == "api" and argv[2] == "graphql"


def lens_node(number: int = PR, head: str = HEAD, state: str = "SUCCESS") -> dict:
    return {
        "number": number,
        "headRefOid": head,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head,
                        "status": {"context": {"state": state, "createdAt": OLD}},
                    }
                }
            ]
        },
    }


def lens_gh(
    nodes: list[dict] | None = None, prs: list[dict] | None = None, **kwargs
) -> FakeGH:
    """PR #7 has a green lens verdict and no sdk-review label."""
    gh = base_gh(prs=prs if prs is not None else [pull(labels=[])], **kwargs)
    gh.on(
        is_graphql,
        ok(
            "\n".join(
                json.dumps(n) for n in (nodes if nodes is not None else [lens_node()])
            )
        ),
    )
    return gh


def run_lens_sweep(gh: FakeGH, api: FakeLensAPI, **kwargs) -> list:
    return run_sweep(gh, lens=api.source(), **kwargs)


# --- the recovery -----------------------------------------------------------


def test_a_lost_lens_approval_is_reposted_by_the_code_owner():
    gh, api = lens_gh(), FakeLensAPI()
    outcomes = run_lens_sweep(gh, api)

    assert [(o.number, o.action, o.source) for o in outcomes] == [
        (PR, reconcile.RECONCILED, reconcile.LENS)
    ]
    # One APPROVE, on the reviewed head, and it is the only thing the PAT did.
    assert api.pat_calls() == [(PAT, "POST", f"/repos/{REPO}/pulls/{PR}/reviews")]
    assert api.approvals() == api.pat_calls()


def test_a_lens_recovery_runs_no_model_and_touches_no_lens_state():
    """It replays the posted verdict: no comment, status or dismissal writes."""
    gh, api = lens_gh(), FakeLensAPI()
    run_lens_sweep(gh, api)

    writes = [c for c in api.calls if c[1] != "GET"]
    assert writes == [(PAT, "POST", f"/repos/{REPO}/pulls/{PR}/reviews")]


def test_a_healthy_lens_pr_costs_nothing_past_the_prefilter():
    """No green lens status on the head: not one lens REST read."""
    gh, api = lens_gh(nodes=[lens_node(state="FAILURE")]), FakeLensAPI()
    outcomes = run_lens_sweep(gh, api)

    assert outcomes == []
    assert api.calls == []


# --- never re-approves when ------------------------------------------------


def test_lens_head_moved_past_the_verdict_is_never_approved():
    api = FakeLensAPI(comments=[lens_summary(lens_state(reviewed_head=OTHER))])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "head moved" in outcomes[0].reason
    assert api.approvals() == []


def test_lens_prefilter_ignores_a_green_status_on_an_older_head():
    """The GraphQL head disagrees with the listing: nothing is read or posted."""
    gh = lens_gh(nodes=[lens_node(head=OTHER)])
    api = FakeLensAPI()
    outcomes = run_lens_sweep(gh, api)

    assert outcomes == []
    assert api.calls == []


def test_a_withdrawn_lens_approval_is_never_replayed():
    """lens withdrew its approval on this head (or a person dismissed it).
    Only a new lens round may approve that head again."""
    api = FakeLensAPI(reviews=[lens_review(state="DISMISSED")])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "withdrawn" in outcomes[0].reason
    assert api.approvals() == []


def test_a_withdrawal_on_an_older_head_does_not_block_the_new_one():
    """The ruleset dismisses approvals on push; that dismissal is about the
    old head, not the head lens has since passed."""
    api = FakeLensAPI(reviews=[lens_review(state="DISMISSED", head=OTHER)])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]


def test_a_withdrawal_landing_after_the_sweep_read_is_still_caught():
    """The approve step re-reads the reviews itself and refuses a replay."""
    api = FakeLensAPI()
    source = api.source()
    verdict = source.verdict(
        pull(labels=[]),
        {(PR, HEAD): OLD},
        min_age=timedelta(minutes=12),
        stale_after=timedelta(minutes=90),
        now=NOW,
    )
    assert isinstance(verdict, reconcile.Owed)
    api.reviews.append(lens_review(state="DISMISSED"))

    action, detail = verdict.post()

    assert action == reconcile.SKIPPED
    assert "withdrawn" in detail
    assert api.approvals() == []


@pytest.mark.parametrize(
    "state, why",
    [
        (
            lens_state(findings=[Finding("a.py", 1, "low", "style", "t", "b", "e")]),
            "open finding",
        ),
        (
            lens_state(findings=[Finding("a.py", 1, "high", "bug", "t", "b", "e")]),
            "open finding",
        ),
        (lens_state(pending_files=["a.py"]), "pending"),
        (lens_state(history=[{"round": 3, "incomplete": True}]), "incomplete"),
    ],
)
def test_lens_is_never_approved_while_anything_is_open(state, why):
    api = FakeLensAPI(comments=[lens_summary(state)])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert why in outcomes[0].reason
    assert api.approvals() == []


def test_a_resolved_finding_does_not_block_the_approval():
    fixed = Finding("a.py", 1, "high", "bug", "t", "b", "e", status="fixed")
    api = FakeLensAPI(comments=[lens_summary(lens_state(findings=[fixed]))])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]


def test_an_existing_lens_approval_on_the_head_is_a_no_op():
    api = FakeLensAPI(reviews=[lens_review()])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert outcomes[0].reason == "already approved"
    assert api.approvals() == []


@pytest.mark.parametrize(
    "status, why",
    [
        # A round is running on this head: never race it.
        (lens_status(state="pending"), "pending"),
        # A round failed or never started (lens withdraws, its state is unchanged).
        (lens_status(state="error"), "error"),
        (lens_status(state="failure"), "failure"),
        # Anyone with statuses:write can set a `lens` status; only lens's App counts.
        (lens_status(creator="github-actions[bot]"), "not lens"),
    ],
)
def test_the_lens_status_must_be_lens_own_green(status, why):
    """The GraphQL prefilter is only a prefilter: the newest status is re-read."""
    api = FakeLensAPI(statuses=[status, lens_status()])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert why in outcomes[0].reason
    assert api.approvals() == []


def test_a_summary_not_posted_by_lens_is_not_trusted():
    api = FakeLensAPI(comments=[lens_summary(login="github-actions[bot]")])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert api.approvals() == []


def test_a_recent_lens_verdict_is_left_to_its_own_run():
    api = FakeLensAPI(statuses=[lens_status(created_at=RECENT)])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "too recent" in outcomes[0].reason
    assert api.approvals() == []


@pytest.mark.parametrize(
    "pr, why",
    [
        ({"state": "closed"}, "closed or a draft"),
        ({"draft": True}, "closed or a draft"),
        ({"user": {"login": "atlan-ci"}}, "authored this PR"),
        ({"head": {"sha": OTHER}}, "head moved"),
    ],
)
def test_the_approve_step_rechecks_the_pr_itself(pr, why):
    api = FakeLensAPI()
    api.pr = {**api.pr, **pr}
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert why in outcomes[0].reason
    assert api.approvals() == []


# --- failures and the shared quota ----------------------------------------


def test_an_unreadable_lens_review_listing_never_approves_blind():
    api = FakeLensAPI()
    api.fail[f"/repos/{REPO}/pulls/{PR}/reviews"] = (502, "bad gateway")
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [(o.action, o.source) for o in outcomes] == [
        (reconcile.DEFERRED, reconcile.LENS)
    ]
    assert api.approvals() == []


def test_an_unreadable_lens_listing_outlasting_a_window_reds_the_run():
    api = FakeLensAPI(statuses=[lens_status(created_at="2026-08-17T09:00:00Z")])
    api.fail[f"/repos/{REPO}/pulls/{PR}/reviews"] = (502, "bad gateway")
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.FAILED]


def test_a_failed_lens_approve_is_a_failure_when_quota_remains():
    api = FakeLensAPI()
    api.approve_response = (422, "Validation Failed")
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.FAILED]
    assert "could not be posted" in outcomes[0].reason


def test_a_lens_approve_losing_the_quota_race_is_a_deferral():
    gh, api = lens_gh(), FakeLensAPI()
    api.approve_response = (403, "API rate limit exceeded")
    reads = {"n": 0}

    def meter():
        reads["n"] += 1
        return ok(f"{0 if reads['n'] > 1 else 4999}\n{RESET}\n")

    gh.on(is_rate_limit, meter)
    outcomes = run_lens_sweep(gh, api)

    assert [o.action for o in outcomes] == [reconcile.DEFERRED]


def test_one_quota_read_covers_both_sources():
    """sdk-review owed on #7, lens owed on #7 too: one meter read, and each
    source posts its own approval (see the module docstring for why)."""
    gh = lens_gh(prs=[pull()])
    api = FakeLensAPI()
    outcomes = run_lens_sweep(gh, api)

    assert [(o.source, o.action) for o in outcomes] == [
        (reconcile.SDK_REVIEW, reconcile.RECONCILED),
        (reconcile.LENS, reconcile.RECONCILED),
    ]
    assert len(gh.called(is_rate_limit)) == 1
    assert len(gh.called(is_approve)) == 1
    assert len(api.approvals()) == 1


def test_a_spent_quota_defers_both_sources_without_a_single_approve():
    gh = lens_gh(prs=[pull()], quota_remaining=0)
    api = FakeLensAPI()
    outcomes = run_lens_sweep(gh, api)

    assert [(o.source, o.action) for o in outcomes] == [
        (reconcile.SDK_REVIEW, reconcile.DEFERRED),
        (reconcile.LENS, reconcile.DEFERRED),
    ]
    assert len(gh.called(is_rate_limit)) == 1
    assert gh.called(is_approve) == []
    assert api.approvals() == []
    assert api.pat_calls() == []


def test_main_stays_green_when_both_sources_defer(monkeypatch):
    monkeypatch.setattr(
        reconcile,
        "sweep",
        lambda *args, **kwargs: [
            reconcile.Outcome(PR, reconcile.DEFERRED, "quota", reconcile.SDK_REVIEW),
            reconcile.Outcome(PR, reconcile.DEFERRED, "quota", reconcile.LENS),
        ],
    )
    assert reconcile.main(["--repo", REPO]) == 0


# --- dry run and reporting ------------------------------------------------


def test_dry_run_lists_what_each_source_would_reconcile(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    gh = lens_gh(prs=[pull()])
    api = FakeLensAPI()
    outcomes = run_lens_sweep(gh, api, dry_run=True)
    reconcile.report(outcomes, REPO)

    assert [(o.source, o.reason) for o in outcomes] == [
        (reconcile.SDK_REVIEW, reconcile.DRY_RUN_REASON),
        (reconcile.LENS, reconcile.DRY_RUN_REASON),
    ]
    assert gh.called(is_approve) == [] and api.approvals() == []
    assert gh.called(is_rate_limit) == []
    written = summary.read_text()
    assert "(sdk-review)" in written and "(lens)" in written


def test_a_lens_recovery_is_annotated_as_lens(capsys):
    reconcile.report(
        [reconcile.Outcome(PR, reconcile.RECONCILED, "approved", reconcile.LENS)],
        REPO,
    )
    out = capsys.readouterr().out
    assert "::warning::" in out and "lens ready-to-merge verdict" in out


# --- the prefilter --------------------------------------------------------


def test_lens_prefilter_failure_is_loud():
    gh = lens_gh()
    gh.on(is_graphql, fail("boom"))

    with pytest.raises(SystemExit, match="failed to list lens verdicts"):
        run_lens_sweep(gh, FakeLensAPI())


def test_lens_prefilter_keeps_only_green_heads():
    gh = FakeGH()
    gh.on(
        is_graphql,
        ok(
            "\n".join(
                json.dumps(n)
                for n in [
                    lens_node(number=1),
                    lens_node(number=2, state="PENDING"),
                    {"number": 3, "headRefOid": HEAD, "commits": {"nodes": []}},
                    {
                        "number": 4,
                        "headRefOid": HEAD,
                        "commits": {
                            "nodes": [{"commit": {"oid": HEAD, "status": None}}]
                        },
                    },
                ]
            )
        ),
    )
    assert reconcile.lens_ready_heads(REPO, gh) == {(1, HEAD): OLD}
    [argv] = gh.called(is_graphql)
    assert "--paginate" in argv and 'context(name: "lens")' in " ".join(argv)


# --- manual dispatch ------------------------------------------------------
#
# A person who dispatches the workflow has seen the approval missing and the
# run finished. The grace exists for the unattended cron; it must never answer
# them "too recent" and skip the PR they asked about.


def _a_minute_ago() -> str:
    """`main()` reads the real clock, so "recent" has to be relative to it."""
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_a_manual_dispatch_reconciles_a_verdict_the_cron_would_call_too_recent():
    gh = base_gh(comments=[comment(created_at=_a_minute_ago())])
    assert reconcile.main(["--repo", REPO], runner=gh) == 0
    assert gh.called(is_approve) == [], "the cron keeps its grace"

    gh = base_gh(comments=[comment(created_at=_a_minute_ago())])
    assert (
        reconcile.main(["--repo", REPO, "--event-name", "workflow_dispatch"], runner=gh)
        == 0
    )
    assert len(gh.called(is_approve)) == 1


def test_a_manual_dispatch_reconciles_a_fresh_lens_verdict():
    api = FakeLensAPI(statuses=[lens_status(created_at=RECENT)])
    outcomes = run_lens_sweep(
        lens_gh(), api, min_age=reconcile.min_age_for("workflow_dispatch", 12)
    )

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]


def test_a_manual_dispatch_still_never_approves_a_withdrawn_or_open_verdict():
    """Only the timer is dropped. Every other guard still holds."""
    min_age = reconcile.min_age_for("workflow_dispatch", 12)
    api = FakeLensAPI(
        statuses=[lens_status(created_at=RECENT)],
        reviews=[lens_review(state="DISMISSED")],
    )
    assert [o.action for o in run_lens_sweep(lens_gh(), api, min_age=min_age)] == [
        reconcile.SKIPPED
    ]
    assert api.approvals() == []

    api = FakeLensAPI(statuses=[lens_status(state="pending", created_at=RECENT)])
    assert [o.action for o in run_lens_sweep(lens_gh(), api, min_age=min_age)] == [
        reconcile.SKIPPED
    ]
    assert api.approvals() == []


def test_the_cron_keeps_the_default_grace():
    assert reconcile.min_age_for("schedule", 12) == timedelta(minutes=12)
    assert reconcile.min_age_for("workflow_dispatch", 12) == timedelta(0)


def test_pr_input_limits_the_sweep_to_that_pr():
    gh = base_gh(prs=[pull(number=3), pull(number=PR)])
    assert (
        reconcile.main(
            ["--repo", REPO, "--event-name", "workflow_dispatch", "--pr", str(PR)],
            runner=gh,
        )
        == 0
    )
    assert len(gh.called(is_approve)) == 1
    assert gh.called(lambda a: a[1] == "api" and "/issues/3/" in a[2]) == []


@pytest.mark.parametrize(
    "value, expected", [("", None), ("  ", None), ("7", 7), ("#7", 7)]
)
def test_pr_input_parsing(value, expected):
    assert reconcile.parse_pr(value) == expected


@pytest.mark.parametrize("value", ["abc", "0", "-3", "7; rm -rf /"])
def test_a_malformed_pr_input_is_refused(value):
    with pytest.raises(SystemExit, match="must be a PR number"):
        reconcile.parse_pr(value)


# --- lens review round 1 (PR #4035) ----------------------------------------


def _owed_lens_verdict(api):
    verdict = api.source().verdict(
        pull(labels=[]),
        {(PR, HEAD): OLD},
        min_age=timedelta(minutes=12),
        stale_after=timedelta(minutes=90),
        now=NOW,
    )
    assert isinstance(verdict, reconcile.Owed)
    return verdict


def test_a_lens_round_finishing_before_the_post_stops_the_replay():
    """F-012fb2: a `/lens` round that started after the sweep's read and ended
    not ready on the same head has no approval to withdraw. Its status is the
    only trace, so it is re-read right before the APPROVE."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.statuses.insert(0, lens_status(state="failure"))

    action, detail = verdict.post()

    assert action == reconcile.SKIPPED
    assert "changed before approval" in detail and "failure" in detail
    assert api.approvals() == []


def test_a_lens_round_still_running_at_post_time_is_not_raced():
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.statuses.insert(0, lens_status(state="pending"))

    assert verdict.post()[0] == reconcile.SKIPPED
    assert api.approvals() == []


def test_an_unreadable_lens_status_at_post_time_defers():
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")

    assert verdict.post()[0] == reconcile.DEFERRED
    assert api.approvals() == []


@pytest.mark.parametrize(
    "path",
    [f"/repos/{REPO}/commits/{HEAD}/statuses", f"/repos/{REPO}/issues/{PR}/comments"],
)
def test_an_unreadable_lens_status_or_summary_defers_instead_of_skipping(path):
    """F-bdac06: a read failure is a blocked approval, loud and able to
    escalate, never a quiet skip on every tick."""
    api = FakeLensAPI()
    api.fail[path] = (502, "bad gateway")
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [(o.action, o.source) for o in outcomes] == [
        (reconcile.DEFERRED, reconcile.LENS)
    ]
    assert "unreadable" in outcomes[0].reason
    assert api.approvals() == []


def _lens_node_at(created_at: str) -> dict:
    node = lens_node()
    node["commits"]["nodes"][0]["commit"]["status"]["context"]["createdAt"] = created_at
    return node


def test_an_unreadable_lens_status_outlasting_a_window_reds_the_run():
    gh = lens_gh(nodes=[_lens_node_at("2026-08-17T09:00:00Z")])
    api = FakeLensAPI()
    api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")
    outcomes = run_lens_sweep(gh, api)

    assert [o.action for o in outcomes] == [reconcile.FAILED]


def test_an_unreadable_lens_status_on_a_fresh_verdict_is_just_too_recent():
    gh = lens_gh(nodes=[_lens_node_at(RECENT)])
    api = FakeLensAPI()
    api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")
    outcomes = run_lens_sweep(gh, api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "too recent" in outcomes[0].reason


def test_a_lens_status_past_the_first_page_is_still_found():
    """F-961972: a head busy with other statuses pushes lens's past page 1."""
    others = [
        {"context": f"ci/{i}", "state": "success", "creator": {"login": "x"}}
        for i in range(150)
    ]
    api = FakeLensAPI(statuses=[*others, lens_status()])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
    pages = [c[2] for c in api.calls if "/statuses" in c[2]]
    assert any(p.endswith("&page=2") for p in pages)


def test_a_lens_status_search_stops_at_the_end_of_the_listing():
    api = FakeLensAPI(statuses=[{"context": "ci/other", "state": "success"}])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.reason for o in outcomes] == ["no lens status on the head"]
    assert len([c for c in api.calls if "/statuses" in c[2]]) == 1


class _KwargsRecorder:
    """Wraps a FakeGH to record the keyword arguments of every call."""

    def __init__(self, gh: FakeGH) -> None:
        self.gh = gh
        self.seen: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.seen.append((list(argv), kwargs))
        return self.gh(argv, **kwargs)


def test_every_gh_call_this_script_makes_is_bounded():
    """F-449b02: a stalled CLI must not hold the run to the job timeout."""
    recorder = _KwargsRecorder(lens_gh(prs=[pull()]))
    reconcile.sweep(
        REPO,
        runner=recorder,
        now=NOW,
        sleeper=lambda _s: None,
        lens=FakeLensAPI().source(),
    )
    own = [
        kwargs
        for argv, kwargs in recorder.seen
        if is_pr_list(argv) or is_graphql(argv) or is_rate_limit(argv)
    ]
    assert len(own) == 3
    assert all(kw.get("timeout") == reconcile.GH_TIMEOUT_SECONDS for kw in own)


def _stall():
    raise subprocess.TimeoutExpired(cmd="gh", timeout=reconcile.GH_TIMEOUT_SECONDS)


def test_a_stalled_pr_listing_is_loud():
    gh = base_gh()
    gh.on(is_pr_list, _stall)

    with pytest.raises(SystemExit, match="timed out"):
        run_sweep(gh)


def test_a_stalled_lens_prefilter_is_loud():
    gh = lens_gh()
    gh.on(is_graphql, _stall)

    with pytest.raises(SystemExit, match="timed out"):
        run_lens_sweep(gh, FakeLensAPI())


def test_a_stalled_quota_read_is_treated_as_unreadable_not_empty():
    """Same as any unreadable meter: go ahead and try the approval."""
    gh = base_gh()
    gh.on(is_rate_limit, _stall)
    outcomes = run_sweep(gh)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]


# --- lens review round 2 (PR #4035) ----------------------------------------


@pytest.mark.parametrize("state", ["pending", "failure", "error"])
def test_a_lens_round_landing_during_the_approve_reads_stops_the_post(state):
    """F-0ff5f4: the status is the last thing read before the POST, after the
    approve step's own reviews and PR reads, not before them."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.on_pr_read = lambda: api.statuses.insert(0, lens_status(state=state))

    action, detail = verdict.post()

    assert action == reconcile.SKIPPED
    assert "changed before approval" in detail and state in detail
    assert api.approvals() == []


def test_an_unreadable_status_during_the_approve_reads_defers():
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)

    def break_statuses():
        api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")

    api.on_pr_read = break_statuses

    assert verdict.post()[0] == reconcile.DEFERRED
    assert api.approvals() == []


def test_the_status_is_read_after_every_other_approve_read():
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.calls.clear()

    assert verdict.post()[0] == reconcile.RECONCILED
    reads = [path for _token, method, path in api.calls if method == "GET"]
    assert "/statuses" in reads[-1]
    assert [c[1] for c in api.calls][-1] == "POST"
