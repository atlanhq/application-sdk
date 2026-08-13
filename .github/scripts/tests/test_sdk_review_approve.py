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
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sdk_review_approve", Path(__file__).resolve().parents[1] / "sdk_review_approve.py"
)
approve = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
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


def test_head_moved_since_review_skips_every_stamp(monkeypatch):
    gh = base_gh()
    assert run_main(gh, monkeypatch, COMMENT_BODY=verdict_comment(head=OTHER)) == 0
    assert gh.called(is_approve) == []
    assert gh.called(is_status) == []
    assert gh.called(is_label_add) == []


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
    # It still reconciles labels (that part is unconditional)...
    assert gh.called(is_label_add)
    # ...but the freshly-added label must not satisfy its own guard.
    assert gh.called(is_approve) == []


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
