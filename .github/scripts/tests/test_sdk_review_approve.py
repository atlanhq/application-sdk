"""Tests for the SDK review verdict stamper.

The regression these pin: a READY_TO_MERGE verdict whose approval POST fails
must NOT leave a green `sdk-review` commit status behind. That combination —
green check, no approving review — is what made a rate-limited approval look
like a completed review on the PR.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sdk_review_approve", Path(__file__).resolve().parents[1] / "sdk_review_approve.py"
)
approve = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
# Registered before exec: @dataclass resolves annotations through
# sys.modules[cls.__module__], which is absent for a bare module_from_spec.
sys.modules["sdk_review_approve"] = approve
SPEC.loader.exec_module(approve)


REPO = "atlanhq/application-sdk"
PR = "7"
HEAD = "a2f276a06384ad38ba3e2e96820a313ed3859db2"
OTHER = "51c160b06a2a350289c7d779f4ab887503f98685"

RATE_LIMIT_STDERR = "gh: API rate limit exceeded for user ID 62283865. (HTTP 403)"


def verdict_comment(verdict: str = "READY_TO_MERGE", head: str = HEAD) -> str:
    return (
        "<!-- SDK_REVIEW -->\n"
        f"<!-- VERDICT: {verdict} -->\n"
        f"<!-- REVIEWED_HEAD: {head} -->\n"
        "## SDK Review (mothership)\n"
    )


def summary_comment(
    comment_id: int,
    login: str = "mothership-ai[bot]",
    body: str = "<!-- SDK_REVIEW -->",
) -> dict:
    """A verdict-summary list entry as the issues comments API returns it."""
    return {"id": comment_id, "body": body, "user": {"login": login}}


def ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr
    )


class FakeGH:
    """Records every `gh` invocation and answers from registered matchers."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.tokens: list[str | None] = []
        self.matchers: list[tuple] = []

    def on(self, predicate, response) -> None:
        """Register a matcher. Later registrations override earlier ones, so a
        test can re-point a path that `base_gh()` already stubbed."""
        self.matchers.append((predicate, response))

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        env = kwargs.get("env") or {}
        self.tokens.append(env.get("GH_TOKEN"))
        for predicate, response in reversed(self.matchers):
            if predicate(argv):
                return response() if callable(response) else response
        return ok()

    # --- assertions helpers ---

    def called(self, predicate) -> list[list[str]]:
        return [argv for argv in self.calls if predicate(argv)]


def is_approve(argv) -> bool:
    return (
        argv[1] == "api"
        and argv[2] == f"repos/{REPO}/pulls/{PR}/reviews"
        and "POST" in argv
    )


def is_review_list(argv) -> bool:
    return (
        argv[1] == "api"
        and argv[2] == f"repos/{REPO}/pulls/{PR}/reviews"
        and "--paginate" in argv
    )


def is_status(argv) -> bool:
    return argv[1] == "api" and argv[2].startswith(f"repos/{REPO}/statuses/")


def is_head_lookup(argv) -> bool:
    return argv[1] == "api" and argv[2] == f"repos/{REPO}/pulls/{PR}"


def is_label_add(argv) -> bool:
    return (
        argv[1] == "api"
        and argv[2] == f"repos/{REPO}/issues/{PR}/labels"
        and "POST" in argv
    )


def is_rate_limit_probe(argv) -> bool:
    return argv[1] == "api" and argv[2] == "rate_limit"


def is_comment_post(argv) -> bool:
    return (
        argv[1] == "api"
        and argv[2] == f"repos/{REPO}/issues/{PR}/comments"
        and "POST" in argv
    )


def posted_comment_body(gh: FakeGH) -> str:
    """The body of the single comment POST `gh` recorded."""
    (argv,) = gh.called(is_comment_post)
    return next(arg[len("body=") :] for arg in argv if arg.startswith("body="))


def starter_comment(
    head: str = HEAD, run_id: str = "99", finished: bool = False
) -> dict:
    """The "review starting" comment `sdk-review.yml` posts, as the API returns it.

    Kept in the shape `sdk_review_gate.inflight_sibling_run` parses, since that
    is the function the stale-head re-review defers to.
    """
    body = (
        "<!-- SDK_REVIEW_STARTED -->\n"
        f"<!-- SDK_REVIEW_STARTED_HEAD: {head} -->\n"
        f"<!-- SDK_REVIEW_STARTED_RUN: {run_id} -->\n"
        "SDK review starting."
    )
    if finished:
        body += "\n\n\u2014 status `success`"
    return {"id": 4, "body": body, "user": {"login": "atlan-ci"}}


def retrigger_comment(head: str = HEAD, login: str = "atlan-ci") -> dict:
    return {
        "id": 6,
        "body": approve.retrigger_body(OTHER, head),
        "user": {"login": login},
    }


def comments(*bodies: dict) -> str:
    return json.dumps([list(bodies)])


def base_gh(
    labels: list[str] | None = None, reviews: list[dict] | None = None
) -> FakeGH:
    gh = FakeGH()
    gh.on(is_head_lookup, ok(HEAD + "\n"))
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(json.dumps([[summary_comment(5)]])),
    )
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}" and "--jq" in a,
        ok("\n".join(labels or [])),
    )
    gh.on(is_review_list, ok(json.dumps([reviews or []])))
    return gh


def run_main(gh: FakeGH, monkeypatch, **env) -> int:
    defaults = {
        "REPO": REPO,
        "PR_NUMBER": PR,
        "COMMENT_BODY": verdict_comment(),
        "TRIGGERING_COMMENT_ID": "5",
        "APPROVER_TOKEN": "pat-atlan-ci",
        "GH_TOKEN": "app-token",
        "APPROVE_MAX_ATTEMPTS": "3",
        "APPROVE_MAX_WAIT_SECONDS": "120",
    }
    defaults.update(env)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    slept: list[float] = []
    code = approve.main(runner=gh, sleeper=slept.append, now=lambda: 1_000.0)
    gh.slept = slept  # type: ignore[attr-defined]
    return code


# --- extract_verdict() ---------------------------------------------------


def test_structured_marker_wins():
    assert approve.extract_verdict(verdict_comment("NEEDS_FIXES")) == "NEEDS_FIXES"


def test_prose_fallback_for_legacy_comments():
    body = "## SDK Review\n\n### Verdict: READY TO MERGE\n\nsome prose"
    assert approve.extract_verdict(body) == "READY_TO_MERGE"


def test_prose_fallback_is_case_insensitive():
    assert approve.extract_verdict("### verdict: needs human") == "NEEDS_HUMAN"


def test_no_verdict_returns_none():
    assert approve.extract_verdict("just a normal comment") is None


def test_reviewed_head_extracted():
    assert approve.extract_reviewed_head(verdict_comment()) == HEAD


def test_missing_reviewed_head_returns_none():
    assert approve.extract_reviewed_head("### Verdict: READY TO MERGE") is None


# --- label_plan() --------------------------------------------------------


def test_ready_adds_approved_and_clears_the_blocking_labels():
    add, remove = approve.label_plan(
        "READY_TO_MERGE", {"sdk-review-needs-human", "sdk-review-needs-rebase"}
    )
    assert add == {"sdk-review-approved"}
    assert remove == {"sdk-review-needs-human", "sdk-review-needs-rebase"}


def test_already_correct_labels_cost_nothing():
    add, remove = approve.label_plan("READY_TO_MERGE", {"sdk-review-approved"})
    assert add == set()
    assert remove == set()


def test_needs_human_clears_approved():
    add, remove = approve.label_plan("NEEDS_HUMAN", {"sdk-review-approved"})
    assert add == {"sdk-review-needs-human"}
    assert remove == {"sdk-review-approved"}


def test_needs_fixes_clears_verdict_labels_but_keeps_rebase():
    add, remove = approve.label_plan(
        "NEEDS_FIXES", {"sdk-review-approved", "sdk-review-needs-rebase"}
    )
    assert add == set()
    assert remove == {"sdk-review-approved"}


def test_legacy_labels_are_stripped_only_when_present():
    _, remove = approve.label_plan("READY_TO_MERGE", {"test-sdk-review-approved"})
    assert remove == {"test-sdk-review-approved"}
    _, absent = approve.label_plan("READY_TO_MERGE", set())
    assert absent == set()


# --- status_for() / is_rate_limited() -----------------------------------


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("READY_TO_MERGE", "success"),
        ("NEEDS_FIXES", "failure"),
        ("NEEDS_HUMAN", "failure"),
        ("BLOCKED", "failure"),
        ("SOMETHING_NEW", "failure"),
    ],
)
def test_status_state_per_verdict(verdict, expected):
    assert approve.status_for(verdict)[0] == expected


def test_rate_limit_detected_from_gh_stderr():
    assert approve.is_rate_limited(RATE_LIMIT_STDERR)
    assert approve.is_rate_limited("You have exceeded a secondary rate limit")
    assert not approve.is_rate_limited("gh: Not Found (HTTP 404)")


# --- post_approval_with_retry() -----------------------------------------


def test_approval_succeeds_first_try_without_sleeping():
    gh = base_gh()
    client = approve.Client(REPO, PR, gh)
    slept: list[float] = []
    assert approve.post_approval_with_retry(
        client, HEAD, "pat", 3, 120, sleeper=slept.append, now=lambda: 0.0
    )
    assert slept == []
    assert len(gh.called(is_approve)) == 1


def test_transient_failure_is_retried_then_succeeds():
    gh = base_gh()
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        return (
            fail("gh: Internal Server Error (HTTP 500)") if attempts["n"] == 1 else ok()
        )

    gh.on(is_approve, flaky)
    client = approve.Client(REPO, PR, gh)
    slept: list[float] = []
    assert approve.post_approval_with_retry(
        client, HEAD, "pat", 3, 120, sleeper=slept.append, now=lambda: 0.0
    )
    assert slept == [5.0]


def test_primary_quota_beyond_budget_fails_fast_without_sleeping():
    """The whole point: don't hold a runner for an hour-away reset."""
    gh = base_gh()
    gh.on(is_approve, fail(RATE_LIMIT_STDERR, code=1))
    gh.on(is_rate_limit_probe, ok("3600"))  # resets 3600s after now=0
    client = approve.Client(REPO, PR, gh)
    slept: list[float] = []
    assert not approve.post_approval_with_retry(
        client, HEAD, "pat", 3, 120, sleeper=slept.append, now=lambda: 0.0
    )
    assert slept == []
    assert len(gh.called(is_approve)) == 1


def test_secondary_rate_limit_inside_budget_waits_for_reset():
    gh = base_gh()
    attempts = {"n": 0}

    def limited():
        attempts["n"] += 1
        return fail(RATE_LIMIT_STDERR) if attempts["n"] == 1 else ok()

    gh.on(is_approve, limited)
    gh.on(is_rate_limit_probe, ok("30"))  # 30s away, inside the 120s budget
    client = approve.Client(REPO, PR, gh)
    slept: list[float] = []
    assert approve.post_approval_with_retry(
        client, HEAD, "pat", 3, 120, sleeper=slept.append, now=lambda: 0.0
    )
    assert slept == [32.0]  # reset + 2s so the window has rolled over


def test_secondary_rate_limit_ignores_the_distant_primary_reset():
    """A secondary throttle must wait its short window, not the hourly core reset.

    Regression: `rate_limit_reset()` reads `.resources.core.reset` — the primary
    window. A secondary limit firing while that reset is far away must NOT be
    failed fast against it; it should retry on a bounded delay and succeed.
    """
    gh = base_gh()
    attempts = {"n": 0}

    def limited():
        attempts["n"] += 1
        # True secondary-limit stderr, and the primary core reset is 3600s away.
        return (
            fail("You have exceeded a secondary rate limit. (HTTP 403)")
            if attempts["n"] == 1
            else ok()
        )

    gh.on(is_approve, limited)
    gh.on(is_rate_limit_probe, ok("3600"))  # distant primary reset — irrelevant
    client = approve.Client(REPO, PR, gh)
    slept: list[float] = []
    assert approve.post_approval_with_retry(
        client, HEAD, "pat", 3, 120, sleeper=slept.append, now=lambda: 0.0
    )
    # Waited the bounded secondary delay (15s), NOT the 3600s primary reset.
    assert slept == [15.0]
    assert len(gh.called(is_approve)) == 2


def test_exhausting_attempts_returns_false():
    gh = base_gh()
    gh.on(is_approve, fail("gh: Internal Server Error (HTTP 500)"))
    client = approve.Client(REPO, PR, gh)
    assert not approve.post_approval_with_retry(
        client, HEAD, "pat", 2, 120, sleeper=lambda _: None, now=lambda: 0.0
    )
    assert len(gh.called(is_approve)) == 2


# --- main(): the ordering guarantee -------------------------------------


def test_failed_approval_leaves_status_unset(monkeypatch):
    gh = base_gh()
    gh.on(is_approve, fail(RATE_LIMIT_STDERR))
    gh.on(is_rate_limit_probe, ok("999999"))
    assert run_main(gh, monkeypatch) == 1
    assert gh.called(is_status) == [], "a failed approval must not go green"


def test_ready_verdict_with_failed_label_add_does_not_approve_or_go_green(monkeypatch):
    """An approval without its `sdk-review-approved` label can never be
    auto-dismissed on a later CI failure (the downgrade workflow gates on the
    label) — so a failed label add must block the approval and the green status.
    """
    gh = base_gh()  # fast path: base_gh labels=[] → sdk-review-approved is in to_add
    gh.on(is_label_add, fail("gh: Resource not accessible by integration (HTTP 403)"))
    assert run_main(gh, monkeypatch) == 1
    assert gh.called(is_approve) == [], "no approval without its label"
    assert gh.called(is_status) == [], "status stays pending, not green"


def test_successful_approval_sets_status_after_approving(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch) == 0
    order = [
        "approve" if is_approve(a) else "status"
        for a in gh.calls
        if is_approve(a) or is_status(a)
    ]
    assert order == ["approve", "status"]


def test_approve_uses_the_pat_and_everything_else_the_app_token(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch) == 0
    pat_calls = [
        argv for argv, token in zip(gh.calls, gh.tokens) if token == "pat-atlan-ci"
    ]
    assert len(pat_calls) == 1, "atlan-ci quota must be spent on exactly one request"
    assert is_approve(pat_calls[0])


def test_non_ready_verdict_sets_failure_status_and_never_approves(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, COMMENT_BODY=verdict_comment("NEEDS_FIXES")) == 0
    assert gh.called(is_approve) == []
    (status,) = gh.called(is_status)
    assert "state=failure" in status


def test_superseded_ready_verdict_dismisses_the_stale_bot_approval(monkeypatch):
    stale = {
        "id": 42,
        "state": "APPROVED",
        "user": {"login": "atlan-ci"},
        "body": approve.APPROVAL_SIGNATURE + " READY TO MERGE.",
    }
    gh = base_gh(reviews=[stale])
    assert run_main(gh, monkeypatch, COMMENT_BODY=verdict_comment("NEEDS_FIXES")) == 0
    assert gh.called(lambda a: "dismissals" in a[2])


# --- the stale-head branch (FND-638) -------------------------------------
#
# The refusal to stamp is correct and pinned below; what these add is that the
# refusal no longer ends the story. Six of sixteen completed runs on one day had
# their verdict discarded here with only a warning, and the loop then waited on
# a human who was never told.


def stale_head_env() -> dict:
    """Env for a run whose verdict describes OTHER while the live head is HEAD."""
    return {"COMMENT_BODY": verdict_comment(head=OTHER)}


def test_head_moved_since_review_skips_every_stamp(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_approve) == []
    assert gh.called(is_status) == []
    assert gh.called(is_label_add) == []


def test_head_moved_requests_a_review_of_the_current_head(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    body = posted_comment_body(gh)
    # `sdk-review.yml`'s job-level `if:` is a startsWith, so the mention has to
    # be the first thing in the body — markers go at the bottom.
    assert body.startswith("@sdk-review")
    assert approve.RETRIGGER_MARKER in body
    assert f"<!-- SDK_REVIEW_RETRIGGER_HEAD: {HEAD} -->" in body


def test_the_review_request_is_posted_as_atlan_ci(monkeypatch):
    """Not under GH_TOKEN: the reviewer's `if:` admits no identity we hold.

    `sdk-review.yml` accepts `atlan-ci`, `mothership-ai[bot]` and human
    collaborators; a trigger from the fleet App would be silently ignored. And
    `sdk-review-dismiss-on-human.yml` excludes `atlan-ci` by name, so this
    comment cannot be mistaken for the human activity that dismisses approvals.
    """
    gh = base_gh()
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    index = gh.calls.index(gh.called(is_comment_post)[0])
    assert gh.tokens[index] == "pat-atlan-ci"


def test_the_review_request_is_posted_once_per_sha(monkeypatch):
    """The once-per-sha key is what stops fix->review->fix becoming a loop."""
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(comments(summary_comment(5), retrigger_comment(head=HEAD))),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_comment_post) == []


def test_a_forged_retrigger_marker_does_not_suppress_the_request(monkeypatch):
    """A marker from a non-`atlan-ci` author is not a request this loop made.

    Anyone can comment on a public-repo PR. Without the author check, a forged
    `SDK_REVIEW_RETRIGGER` marker for the current head would read as
    `already-requested` and silently suppress the fresh review the stale-head
    refusal exists to ask for — the exact failure FND-638 was fixing.
    """
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(
            comments(
                summary_comment(5), retrigger_comment(head=HEAD, login="evil-doer")
            )
        ),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert f"<!-- SDK_REVIEW_RETRIGGER_HEAD: {HEAD} -->" in posted_comment_body(gh)


def test_a_request_for_a_different_sha_does_not_block_this_one(monkeypatch):
    """Keyed on the head, not on "a request was made once on this PR"."""
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(comments(summary_comment(5), retrigger_comment(head=OTHER))),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert f"<!-- SDK_REVIEW_RETRIGGER_HEAD: {HEAD} -->" in posted_comment_body(gh)


def test_no_request_while_a_run_is_already_reviewing_that_head(monkeypatch):
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(comments(summary_comment(5), starter_comment(head=HEAD))),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_comment_post) == []


def test_a_finished_run_on_that_head_does_not_count_as_in_flight(monkeypatch):
    """A stamped starter means that run reached its end — it will post nothing more."""
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(comments(summary_comment(5), starter_comment(head=HEAD, finished=True))),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_comment_post) != []


def test_no_request_when_the_current_head_already_has_a_verdict(monkeypatch):
    """The fast path can be driven by an older verdict than the PR's newest."""
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(comments(summary_comment(9, body=verdict_comment(head=HEAD)))),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_comment_post) == []


def test_no_request_when_the_comment_listing_cannot_be_read(monkeypatch):
    """Fails closed: an unreadable listing cannot prove a request is absent.

    A duplicate `@sdk-review` costs a full sandbox run; a lost one costs a human
    typing the mention.
    """
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        fail("gh: Bad gateway (HTTP 502)"),
    )
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_comment_post) == []


def test_no_request_without_an_approver_token(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, APPROVER_TOKEN="", **stale_head_env()) == 0
    assert gh.called(is_comment_post) == []


def test_a_verdict_without_a_reviewed_head_marker_requests_nothing(monkeypatch):
    """A different fault: no sha is established, so there is none to review."""
    gh = base_gh()
    body = "<!-- SDK_REVIEW -->\n<!-- VERDICT: READY_TO_MERGE -->\n"
    assert run_main(gh, monkeypatch, COMMENT_BODY=body) == 0
    assert gh.called(is_comment_post) == []


def test_an_unresolvable_head_requests_nothing(monkeypatch):
    """Also a different fault — and the sha to review is exactly what is missing."""
    gh = base_gh()
    gh.on(is_head_lookup, fail("gh: Bad gateway (HTTP 502)"))
    assert run_main(gh, monkeypatch, **stale_head_env()) == 0
    assert gh.called(is_comment_post) == []


# --- request helpers in isolation ----------------------------------------


def test_retrigger_posted_for_ignores_a_marker_without_a_head_stamp():
    assert not approve.retrigger_posted_for([{"body": approve.RETRIGGER_MARKER}], HEAD)


def test_reviewed_at_ignores_a_forged_verdict_from_another_author():
    forged = summary_comment(11, login="attacker", body=verdict_comment(head=HEAD))
    assert not approve.reviewed_at([forged], HEAD)


def test_newer_verdict_comment_supersedes_this_run(monkeypatch):
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(json.dumps([[summary_comment(99)]])),
    )
    assert run_main(gh, monkeypatch, TRIGGERING_COMMENT_ID="5") == 0
    assert gh.called(is_approve) == []
    assert gh.called(is_status) == []


def test_forged_verdict_comment_from_another_author_does_not_supersede(monkeypatch):
    """A non-bot comment carrying the SDK_REVIEW marker is not a verdict.

    Without the author check, an attacker (or a compromised non-atlan-ci token)
    could post a forged `<!-- SDK_REVIEW -->` comment with a high id and have it
    treated as the newest verdict — altering the supersede decision the script
    makes about the genuine bot verdict.
    """
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(json.dumps([[summary_comment(5), summary_comment(99, login="evil-doer")]])),
    )
    # The forged id=99 comment must be ignored, so the triggering comment (id=5)
    # remains the newest verdict and the run proceeds to approve.
    assert run_main(gh, monkeypatch, TRIGGERING_COMMENT_ID="5") == 0
    assert len(gh.called(is_approve)) == 1


def test_markerless_bot_comment_is_not_a_verdict():
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(
            json.dumps(
                [
                    [
                        {
                            "id": 9,
                            "body": "no marker",
                            "user": {"login": "mothership-ai[bot]"},
                        }
                    ]
                ]
            )
        ),
    )
    assert approve.Client(REPO, PR, gh).newest_summary_comment_id() == 0


def test_existing_bot_approval_is_not_duplicated(monkeypatch):
    existing = {
        "id": 1,
        "state": "APPROVED",
        "user": {"login": "atlan-ci"},
        "body": approve.APPROVAL_SIGNATURE + " READY TO MERGE.",
    }
    gh = base_gh(reviews=[existing])
    assert run_main(gh, monkeypatch) == 0
    assert gh.called(is_approve) == []
    # The status still resolves — the stamp is idempotent, not skipped wholesale.
    assert len(gh.called(is_status)) == 1


def test_human_approval_does_not_count_as_the_bot_approval(monkeypatch):
    human = {
        "id": 2,
        "state": "APPROVED",
        "user": {"login": "cmgrote"},
        "body": "lgtm",
    }
    gh = base_gh(reviews=[human])
    assert run_main(gh, monkeypatch) == 0
    assert len(gh.called(is_approve)) == 1


def test_missing_approver_token_fails_loudly(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, APPROVER_TOKEN="") == 1
    assert gh.called(is_status) == []


def test_unparseable_verdict_is_a_no_op(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, COMMENT_BODY="hello") == 0
    assert gh.calls == []


# --- slow path (sdk-review.yml) -----------------------------------------


def slow_env(**overrides) -> dict:
    """The slow path's configuration: fetch the summary, don't own the status."""
    env = {
        "COMMENT_BODY": "",
        "EXPECTED_HEAD": HEAD,
        "WRITE_STATUS": "false",
        "REQUIRE_APPROVED_LABEL": "true",
    }
    env.update(overrides)
    return env


def slow_gh(labels: list[str] | None = None, **kwargs) -> FakeGH:
    """Like base_gh, but the comment list carries a full verdict body."""
    gh = base_gh(labels=labels, **kwargs)
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(json.dumps([[summary_comment(5, body=verdict_comment())]])),
    )
    return gh


def test_slow_path_reads_the_verdict_off_the_newest_comment(monkeypatch):
    gh = slow_gh(labels=["sdk-review-approved"])
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert len(gh.called(is_approve)) == 1


def test_slow_path_does_not_write_the_commit_status(monkeypatch):
    gh = slow_gh(labels=["sdk-review-approved"])
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert gh.called(is_status) == [], "sdk-review.yml owns its own status writes"


def test_slow_path_picks_the_newest_summary_not_the_last_in_page_order():
    gh = FakeGH()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(
            json.dumps(
                [
                    [
                        summary_comment(9, body=verdict_comment("READY_TO_MERGE")),
                        summary_comment(4, body=verdict_comment("NEEDS_FIXES")),
                    ]
                ]
            )
        ),
    )
    body = approve.Client(REPO, PR, gh).latest_summary_body()
    assert approve.extract_verdict(body) == "READY_TO_MERGE"


def test_slow_path_skips_when_no_summary_comment_exists(monkeypatch):
    gh = base_gh()
    gh.on(
        lambda a: a[2] == f"repos/{REPO}/issues/{PR}/comments",
        ok(json.dumps([[]])),
    )
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert gh.called(is_approve) == []


def test_slow_path_refuses_when_the_approved_label_was_already_stripped(monkeypatch):
    """dismiss-on-human / downgrade / reset-on-push all clear the label."""
    gh = slow_gh(labels=[])
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert gh.called(is_approve) == []


def test_label_guard_reads_the_snapshot_not_the_label_it_just_wrote(monkeypatch):
    """The shell version added the label, then read it back and always passed.

    The guard must consult the state from BEFORE this run reconciled labels, or
    it can never fire and the slow path re-approves after a downgrade.
    """
    gh = slow_gh(labels=[])
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert gh.called(is_approve) == []


def test_a_fired_label_guard_does_not_resurrect_the_stripped_label(monkeypatch):
    """Bailing must happen before the label reconcile, not after it.

    `sdk-review-approved` is what every invalidator strips and what
    sdk_review_reconcile.py's cron gates on. Re-adding it on the way to
    declining the approval would leave the PR wearing a label with nothing
    behind it, and the next reconciler tick would read that as a lost stamp and
    approve a verdict a human had deliberately cleared.
    """
    gh = slow_gh(labels=[])
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert gh.called(is_label_add) == []
    assert gh.called(lambda a: "DELETE" in a) == []


def test_fast_path_does_not_require_the_label(monkeypatch):
    """The fast path is the one that STAMPS the label, so it cannot demand it."""
    gh = base_gh(labels=[])
    assert run_main(gh, monkeypatch) == 0
    assert len(gh.called(is_approve)) == 1


def test_slow_path_rescues_a_fast_path_that_labelled_but_failed_to_approve(monkeypatch):
    """Fast path wrote the label then took a 403 — the label still stands, so
    the slow path is entitled to post the approval it could not."""
    gh = slow_gh(labels=["sdk-review-approved"], reviews=[])
    assert run_main(gh, monkeypatch, **slow_env()) == 0
    assert len(gh.called(is_approve)) == 1


def test_slow_path_head_moved_since_dispatch_skips(monkeypatch):
    gh = slow_gh(labels=["sdk-review-approved"])
    assert run_main(gh, monkeypatch, **slow_env(EXPECTED_HEAD=OTHER)) == 0
    assert gh.called(is_approve) == []
    assert gh.called(is_label_add) == []


def test_slow_path_skips_freshness_check_since_it_fetched_the_newest(monkeypatch):
    """TRIGGERING_COMMENT_ID is meaningless when we just read the newest."""
    gh = slow_gh(labels=["sdk-review-approved"])
    assert run_main(gh, monkeypatch, **slow_env(), TRIGGERING_COMMENT_ID="0") == 0
    assert len(gh.called(is_approve)) == 1


# --- an unreadable review listing must fail CLOSED ------------------------
#
# Regression from 2026-08-17: GitHub's reviews endpoint began returning 404 for
# PRs that plainly had reviews. `_paginated` collapsed that to `[]`, which reads
# as "no approval exists" — the precondition for posting one — so `atlan-ci`
# re-approved the same PR on every reconciler tick, silently.


def test_bot_approval_ids_is_none_when_the_listing_fails():
    """None and [] must not be the same value: one means "cannot tell"."""
    gh = base_gh()
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))
    assert approve.Client(REPO, PR, gh).bot_approval_ids() is None


def test_bot_approval_ids_is_none_when_the_listing_is_unparseable():
    gh = base_gh()
    gh.on(is_review_list, ok("{not json"))
    assert approve.Client(REPO, PR, gh).bot_approval_ids() is None


def test_ready_refuses_to_approve_when_the_listing_is_unreadable(monkeypatch, capsys):
    gh = base_gh(labels=["sdk-review-approved"])
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))

    assert run_main(gh, monkeypatch) == 1
    assert gh.called(is_approve) == [], "approving blind is how outages duplicate"
    assert gh.called(is_status) == []
    assert "::error::" in capsys.readouterr().out


def test_non_ready_fails_loudly_when_stale_approvals_cannot_be_listed(monkeypatch):
    """Failing to dismiss leaves the merge gate open on a superseded approval.

    The run still writes the failure status: returning before the write would
    let a prior green `sdk-review` status on this head outlive the verdict that
    superseded it. The status POST does not depend on the reviews listing, so
    one degradation does not excuse the other silence."""
    gh = base_gh()
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))

    code = run_main(gh, monkeypatch, COMMENT_BODY=verdict_comment("NEEDS_FIXES"))
    assert code == 1
    assert gh.called(lambda a: "dismissals" in a[2]) == []
    (status,) = gh.called(is_status)
    assert "state=failure" in status


def test_non_ready_unreadable_listing_respects_write_status_false(monkeypatch):
    """The slow path (WRITE_STATUS=false) owns no status writes, even here."""
    gh = base_gh()
    gh.on(is_review_list, fail("gh: Not Found (HTTP 404)"))

    code = run_main(
        gh,
        monkeypatch,
        COMMENT_BODY=verdict_comment("NEEDS_FIXES"),
        WRITE_STATUS="false",
    )
    assert code == 1
    assert gh.called(is_status) == []


# --- stamp_verdict() reports what it did ----------------------------------


def test_stamp_verdict_reports_an_approval(monkeypatch):
    gh = base_gh(labels=["sdk-review-approved"])
    for key, value in {
        "REPO": REPO,
        "PR_NUMBER": PR,
        "COMMENT_BODY": verdict_comment(),
        "TRIGGERING_COMMENT_ID": "5",
        "APPROVER_TOKEN": "pat-atlan-ci",
        "GH_TOKEN": "app-token",
    }.items():
        monkeypatch.setenv(key, value)

    outcome = approve.stamp_verdict(runner=gh)
    assert outcome.action == approve.APPROVED
    assert outcome.exit_code == 0


def test_stamp_verdict_distinguishes_a_declining_guard_from_an_approval(monkeypatch):
    """Both exit 0; only the action separates them. That distinction is what the
    reconciler reports on, and inferring it from a re-read was racy."""
    gh = slow_gh(labels=[])
    for key, value in {
        "REPO": REPO,
        "PR_NUMBER": PR,
        "COMMENT_BODY": "",
        "EXPECTED_HEAD": HEAD,
        "REQUIRE_APPROVED_LABEL": "true",
        "APPROVER_TOKEN": "pat-atlan-ci",
        "GH_TOKEN": "app-token",
    }.items():
        monkeypatch.setenv(key, value)

    outcome = approve.stamp_verdict(runner=gh)
    assert outcome.action == approve.SKIPPED
    assert outcome.exit_code == 0
    assert "label" in outcome.detail


def test_label_delete_404_is_tolerated(capsys):
    gh = FakeGH()
    gh.on(lambda a: "DELETE" in a, fail("gh: Not Found (HTTP 404)"))
    approve.Client(REPO, PR, gh).remove_label("sdk-review-approved")
    assert "::warning::" not in capsys.readouterr().out


def test_label_write_failure_is_surfaced_not_swallowed(capsys):
    """A token without `issues: write` must not fail silently."""
    gh = FakeGH()
    gh.on(is_label_add, fail("gh: Resource not accessible by integration (HTTP 403)"))
    approve.Client(REPO, PR, gh).add_labels({"sdk-review-approved"})
    assert "::warning::" in capsys.readouterr().out
