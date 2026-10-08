"""Tests for the review approval reconciler.

The regression these pin: a PR whose lens verdict still stands but whose
`atlan-ci` approval was lost (rate-limited approve step) must get one posted
without a human re-running a job — and *only* such a PR. Every guard that stops
the reconciler blessing something it should not is asserted here, because the
thing it drives is a CODEOWNER approval on `main`.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

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


reconcile = _load("review_approval_reconcile")


REPO = "atlanhq/application-sdk"
PR = 7
HEAD = "a2f276a06384ad38ba3e2e96820a313ed3859db2"
OTHER = "51c160b06a2a350289c7d779f4ab887503f98685"

APP_TOKEN = "app-token"
PAT = "pat-atlan-ci"

NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)
OLD = "2026-08-17T11:00:00Z"  # an hour before NOW
RECENT = (
    "2026-08-17T11:59:00Z"  # a minute before NOW — its own run may still be in flight
)

RATE_LIMIT_STDERR = "gh: API rate limit exceeded for user ID 62283865. (HTTP 403)"


def ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr
    )


def pull(
    number: int = PR,
    head: str = HEAD,
    labels: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "head": {"sha": head},
        "labels": [{"name": name} for name in labels or []],
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


def is_rate_limit(argv) -> bool:
    return argv[1] == "api" and argv[2] == "rate_limit"


RESET = int(NOW.timestamp()) + 1800  # half an hour out


def base_gh(
    prs: list[dict] | None = None,
    quota_remaining: int = 4999,
    quota_reset: int = RESET,
) -> FakeGH:
    """A repo whose open PRs are `prs` (PR #7 alone by default)."""
    gh = FakeGH()
    gh.on(is_rate_limit, lambda: ok(f"{quota_remaining}\n{quota_reset}\n"))
    gh.on(
        is_pr_list,
        ok("\n".join(json.dumps(pr) for pr in (prs if prs is not None else [pull()]))),
    )
    return gh


@pytest.fixture(autouse=True)
def _tokens(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", APP_TOKEN)
    monkeypatch.setenv("APPROVER_TOKEN", PAT)


def run_sweep(gh: FakeGH, **kwargs) -> list:
    return reconcile.sweep(REPO, runner=gh, now=NOW, **kwargs)


# --- reporting and exit status ------------------------------------------


def test_main_stays_green_on_a_deferral(monkeypatch):
    monkeypatch.setattr(
        reconcile,
        "sweep",
        lambda *args, **kwargs: [
            reconcile.Outcome(PR, reconcile.DEFERRED, "atlan-ci quota exhausted")
        ],
    )
    assert reconcile.main(["--repo", REPO]) == 0


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


def test_pr_listing_failure_is_loud():
    gh = FakeGH()
    gh.on(is_pr_list, fail("boom"))

    with pytest.raises(SystemExit, match="failed to list open PRs"):
        run_sweep(gh)


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
        # Runs once the APPROVE has landed: a round finishing while it was in
        # flight.
        self.on_post = None
        self.on_sleep = None
        self.slept: list[float] = []
        self.dismissed: list[tuple[str, int]] = []

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
                    if self.on_post is not None:
                        self.on_post()
                    return status, json.dumps({"id": 99})
                return status, text
            dismissal = f"{base}/pulls/{PR}/reviews/"
            if method == "PUT" and path.startswith(dismissal):
                review_id = int(path[len(dismissal) :].split("/")[0])
                for review in self.reviews:
                    if review["id"] == review_id:
                        review["state"] = "DISMISSED"
                self.dismissed.append((token, review_id))
                return 200, "{}"
            raise AssertionError(f"unexpected lens call: {method} {path}")

        return call

    def source(self):
        return reconcile.LensSource(
            LensGitHub(REPO, token=APP_TOKEN, transport=self.transport(APP_TOKEN)),
            LensGitHub(REPO, token=PAT, transport=self.transport(PAT)),
            sleeper=self.sleep,
        )

    def sleep(self, seconds: float) -> None:
        """Records the confirm delay instead of spending it; `on_sleep` is
        what lands on GitHub during it."""
        self.slept.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep()

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
    """PR #7 has a green lens verdict."""
    gh = base_gh(prs=prs, **kwargs)
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


def test_a_fresh_lens_verdict_is_reconciled_without_waiting():
    api = FakeLensAPI(statuses=[lens_status(created_at=RECENT)])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
    assert len(api.approvals()) == 1


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
    assert "approval step failed" in outcomes[0].reason


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


def test_a_spent_quota_defers_without_a_single_approve():
    gh = lens_gh(quota_remaining=0)
    api = FakeLensAPI()
    outcomes = run_lens_sweep(gh, api)

    assert [(o.source, o.action) for o in outcomes] == [
        (reconcile.LENS, reconcile.DEFERRED)
    ]
    assert "resets in 30min" in outcomes[0].reason
    assert len(gh.called(is_rate_limit)) == 1
    assert api.approvals() == []
    assert api.pat_calls() == []


# --- the settle step, on a stub source ------------------------------------
#
# Quota, deferral and staleness are the sweep's, not the source's. A stub that
# says every PR is owed pins them without any source's own reads.


class OwedSource:
    """Says every PR is owed an approval of `age`; `post` answers `result`."""

    def __init__(
        self,
        age: timedelta = timedelta(minutes=1),
        result: tuple[str, str] = (reconcile.RECONCILED, "approved"),
    ) -> None:
        self.age = age
        self.result = result
        self.posted: list[int] = []

    def verdict(self, pr, ready, *, stale_after, now):
        number = pr["number"]

        def post() -> tuple[str, str]:
            self.posted.append(number)
            return self.result

        return reconcile.Owed(number, reconcile.LENS, self.age, post)


def test_exhausted_quota_spends_no_approve_request_at_all():
    """The original shape discovered exhaustion by taking a 403 — a doomed
    request to learn what a free one already knows. Repeatedly hammering an
    exhausted primary limit is also how it escalates to an abuse block."""
    gh, source = base_gh(quota_remaining=0), OwedSource()
    outcomes = run_sweep(gh, lens=source)

    assert [o.action for o in outcomes] == [reconcile.DEFERRED]
    assert source.posted == []
    assert gh.approver_calls() == [
        argv for argv in gh.calls if is_rate_limit(argv)
    ], "the only atlan-ci request should be the free meter read"


def test_deferral_names_the_reset_and_does_not_red_the_run(capsys):
    """Deferring is the self-healing case: the next tick after the reset posts
    it. A red run every ten minutes for an hour would bury the annotation that
    matters when it does NOT clear."""
    outcomes = run_sweep(base_gh(quota_remaining=0), lens=OwedSource())
    reconcile.report(outcomes, REPO)

    assert "resets in 30min" in outcomes[0].reason
    out = capsys.readouterr().out
    assert "::warning::" in out
    assert "::error::" not in out


def test_a_verdict_outliving_a_full_quota_window_reds_the_run():
    """Past one hourly reset, "waiting for quota" stops being an explanation."""
    source = OwedSource(age=timedelta(hours=3))
    outcomes = run_sweep(base_gh(quota_remaining=0), lens=source)

    assert [o.action for o in outcomes] == [reconcile.FAILED]
    assert "outlasted a full quota window" in outcomes[0].reason
    assert source.posted == []


def test_quota_is_read_once_per_run_not_once_per_pr():
    gh = base_gh(
        quota_remaining=0,
        prs=[pull(number=PR), pull(number=11), pull(number=12)],
    )
    run_sweep(gh, lens=OwedSource())

    assert len(gh.called(is_rate_limit)) == 1


def test_no_candidates_means_no_quota_read():
    """A sweep with nothing to approve must not spend a request establishing
    that it could have."""
    gh = lens_gh()
    run_lens_sweep(gh, FakeLensAPI(reviews=[lens_review()]))

    assert gh.called(is_rate_limit) == []


def test_unreadable_quota_still_attempts_the_approval():
    """Failing to read the meter is not evidence the tank is empty."""
    gh, source = base_gh(), OwedSource()
    gh.on(is_rate_limit, fail("network go boom"))
    outcomes = run_sweep(gh, lens=source)

    assert [o.action for o in outcomes] == [reconcile.RECONCILED]
    assert source.posted == [PR]


# --- dry run and reporting ------------------------------------------------


def test_dry_run_lists_what_would_be_reconciled(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    gh = lens_gh()
    api = FakeLensAPI()
    outcomes = run_lens_sweep(gh, api, dry_run=True)
    reconcile.report(outcomes, REPO)

    assert [(o.source, o.reason) for o in outcomes] == [
        (reconcile.LENS, reconcile.DRY_RUN_REASON),
    ]
    assert api.approvals() == []
    assert gh.called(is_rate_limit) == []
    assert "(lens)" in summary.read_text()


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


# --- no minimum age, anywhere ---------------------------------------------
#
# The grace used to apply to the cron and was skipped only on a manual
# dispatch, so the unattended path -- the one that matters -- still stalled.
# These pin that it is gone from every path, and cannot come back as a flag.


def _a_minute_ago() -> str:
    """`main()` reads the real clock, so "recent" has to be relative to it."""
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _use_source(monkeypatch, source) -> None:
    """`main()` builds its own source from the environment; hand it `source`."""
    monkeypatch.setattr(
        reconcile.LensSource, "from_env", staticmethod(lambda _repo: source)
    )


def test_a_scheduled_run_reconciles_a_minute_old_verdict(monkeypatch):
    api = FakeLensAPI(statuses=[lens_status(created_at=_a_minute_ago())])
    _use_source(monkeypatch, api.source())
    assert reconcile.main(["--repo", REPO], runner=lens_gh()) == 0
    assert len(api.approvals()) == 1


@pytest.mark.parametrize(
    "flag", [["--min-age-minutes", "12"], ["--event-name", "schedule"]]
)
def test_the_grace_flags_are_gone(flag):
    """A flag that could reintroduce the wait is refused, not ignored."""
    with pytest.raises(SystemExit):
        reconcile.main(["--repo", REPO, *flag], runner=base_gh())


def test_no_grace_still_never_approves_a_withdrawn_or_open_lens_verdict():
    """Only the timer is gone. Every other guard still holds on a fresh verdict."""
    api = FakeLensAPI(
        statuses=[lens_status(created_at=RECENT)],
        reviews=[lens_review(state="DISMISSED")],
    )
    assert [o.action for o in run_lens_sweep(lens_gh(), api)] == [reconcile.SKIPPED]
    assert api.approvals() == []

    api = FakeLensAPI(statuses=[lens_status(state="pending", created_at=RECENT)])
    assert [o.action for o in run_lens_sweep(lens_gh(), api)] == [reconcile.SKIPPED]
    assert api.approvals() == []


# --- the workflow runs when a review finishes --------------------------------

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_the_reconciler_runs_when_the_review_source_completes():
    """`workflow_run` matches on the source workflow's `name:`, so a rename of
    lens would silently turn this back into cron-only recovery."""
    wf = _workflow("review-approval-reconcile.yml")
    on = wf.get(True, wf.get("on"))
    listened = set(on["workflow_run"]["workflows"])
    assert on["workflow_run"]["types"] == ["completed"]
    assert listened == {_workflow("lens.yml")["name"]}


def test_skipped_source_runs_do_not_start_a_sweep():
    job = _workflow("review-approval-reconcile.yml")["jobs"]["reconcile"]
    assert "workflow_run.conclusion != 'skipped'" in job["if"]
    assert "github.event_name != 'workflow_run'" in job["if"]


def test_the_workflow_no_longer_passes_the_grace_flags():
    text = (WORKFLOWS / "review-approval-reconcile.yml").read_text()
    assert "--event-name" not in text and "--min-age" not in text


def test_pr_input_limits_the_sweep_to_that_pr(monkeypatch):
    source = OwedSource()
    _use_source(monkeypatch, source)
    gh = base_gh(prs=[pull(number=3), pull(number=PR)])
    assert reconcile.main(["--repo", REPO, "--pr", str(PR)], runner=gh) == 0
    assert source.posted == [PR]


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


@pytest.mark.parametrize("created_at", ["not-a-date", ""])
def test_a_lens_status_with_an_unreadable_timestamp_is_skipped(created_at):
    """With no grace the age only feeds the stale path, but a status whose age
    cannot be read still must not be approved on (F-fcf2bd)."""
    api = FakeLensAPI(statuses=[lens_status(created_at=created_at)])
    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert outcomes[0].reason == "lens status has no readable timestamp"
    assert api.approvals() == []


def test_an_unreadable_lens_status_with_no_readable_prefilter_time_is_skipped():
    """The unreadable-status path falls back on the prefilter's timestamp; when
    that is unreadable too, it skips rather than approving or deferring."""
    gh = lens_gh(nodes=[_lens_node_at("not-a-date")])
    api = FakeLensAPI()
    api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")
    outcomes = run_lens_sweep(gh, api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert outcomes[0].reason == "lens status has no readable timestamp"
    assert api.approvals() == []


def test_an_unreadable_lens_status_on_a_fresh_verdict_is_deferred_not_failed():
    gh = lens_gh(nodes=[_lens_node_at(RECENT)])
    api = FakeLensAPI()
    api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")
    outcomes = run_lens_sweep(gh, api)

    assert [o.action for o in outcomes] == [reconcile.DEFERRED]
    assert "unreadable" in outcomes[0].reason


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
    recorder = _KwargsRecorder(lens_gh())
    reconcile.sweep(REPO, runner=recorder, now=NOW, lens=FakeLensAPI().source())
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
    outcomes = run_sweep(gh, lens=OwedSource())

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


def test_the_status_brackets_the_post():
    """Read last before the POST (after the reviews and PR reads), and first
    after it."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.calls.clear()

    assert verdict.post()[0] == reconcile.RECONCILED
    kinds = [
        "status" if "/statuses" in path else method
        for _token, method, path in api.calls
    ]
    post = kinds.index("POST")
    assert kinds[post - 1] == "status" and kinds[post + 1] == "status"
    assert "status" not in kinds[: post - 1], "no stale early read left over"


# --- lens review round 3 (PR #4035) ----------------------------------------


@pytest.mark.parametrize("state", ["pending", "failure", "error"])
def test_a_lens_round_finishing_while_the_post_is_in_flight_is_undone(state):
    """F-2c4c08: a round can start, publish not ready and run its withdraw
    (finding nothing) while the APPROVE is still in flight. No read before the
    POST can see that, so the verdict is read again after it, and the approval
    just posted is dismissed."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.on_post = lambda: api.statuses.insert(0, lens_status(state=state))

    action, detail = verdict.post()

    assert action == reconcile.SKIPPED
    assert "withdrew the approval just posted" in detail and state in detail
    assert api.dismissed == [(APP_TOKEN, 99)], "dismissed by the App, not the PAT"
    assert [r["state"] for r in api.reviews] == ["DISMISSED"]


def test_an_undone_replay_is_never_retried_by_a_later_tick():
    """The dismissal leaves a withdrawn lens approval on the head, which the
    solo-approval guard reads as "only a new lens round may approve"."""
    api = FakeLensAPI()
    api.on_post = lambda: api.statuses.insert(0, lens_status(state="failure"))
    run_lens_sweep(lens_gh(), api)
    api.on_post = None
    api.statuses = [lens_status()]  # even if the status went green again
    api.calls.clear()

    outcomes = run_lens_sweep(lens_gh(), api)

    assert [o.action for o in outcomes] == [reconcile.SKIPPED]
    assert "withdrawn" in outcomes[0].reason
    assert api.approvals() == []


def test_a_new_ready_round_during_the_post_keeps_the_approval():
    """A newer green status from lens is a verdict that still stands."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.on_post = lambda: api.statuses.insert(0, lens_status())

    assert verdict.post()[0] == reconcile.RECONCILED
    assert api.dismissed == []


def test_an_unreadable_status_after_the_post_keeps_the_approval_and_says_so():
    """Dismissing on an unreadable read would leave a withdrawn approval that
    blocks this replay for good; the approval stays and the detail says it was
    not re-confirmed."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)

    def break_statuses():
        api.fail[f"/repos/{REPO}/commits/{HEAD}/statuses"] = (502, "bad gateway")

    api.on_post = break_statuses

    action, detail = verdict.post()

    assert action == reconcile.RECONCILED
    assert "could not re-confirm" in detail
    assert api.dismissed == []


def test_a_failed_undo_is_a_failure_not_a_recovery():
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)

    def verdict_changes_and_dismissal_breaks():
        api.statuses.insert(0, lens_status(state="failure"))
        api.fail[f"/repos/{REPO}/pulls/{PR}/reviews/99/dismissals"] = (500, "boom")

    api.on_post = verdict_changes_and_dismissal_breaks

    action, detail = verdict.post()

    assert action == reconcile.FAILED
    assert "approval step failed" in detail


def test_lens_own_last_step_does_not_re_read_after_posting():
    """`still_ready` is the reconciler's; lens's own step acts on a fresh
    verdict and keeps its one-POST shape."""
    api = FakeLensAPI()
    gh = LensGitHub(REPO, token=APP_TOKEN, transport=api.transport(APP_TOKEN))
    approver = LensGitHub(REPO, token=PAT, transport=api.transport(PAT))

    out = lens_approve.apply(
        gh, approver, {"action": "approve", "pr": PR, "head": HEAD, "round": 3}
    )

    assert out.startswith("approved ")
    assert [c for c in api.calls if "/statuses" in c[2]] == []


# --- lens review round 4 (PR #4035) ----------------------------------------


def test_the_post_check_waits_out_github_read_after_write_lag():
    """F-2c4c08, the part a bare re-read misses: a round's not-ready status
    published just before an immediate re-read can be invisible to it, and
    that round's withdraw can miss the new approval the same way. The second
    read is taken LENS_CONFIRM_DELAY_SECONDS after the POST."""
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    # Visible only once the delay has run: an immediate re-read would miss it.
    api.on_sleep = lambda: api.statuses.insert(0, lens_status(state="failure"))

    action, detail = verdict.post()

    assert api.slept == [reconcile.LENS_CONFIRM_DELAY_SECONDS]
    assert action == reconcile.SKIPPED
    assert "withdrew the approval just posted" in detail
    assert api.dismissed == [(APP_TOKEN, 99)]


def test_the_delay_comes_between_the_post_and_the_second_read():
    api = FakeLensAPI()
    verdict = _owed_lens_verdict(api)
    api.calls.clear()
    order: list[str] = []
    api.on_post = lambda: order.append("post")
    api.on_sleep = lambda: order.append(f"sleep ({len(api.calls)} calls so far)")

    assert verdict.post()[0] == reconcile.RECONCILED

    kinds = [
        "status" if "/statuses" in path else method
        for _token, method, path in api.calls
    ]
    calls_before_sleep = int(order[1].split("(")[1].split()[0])
    assert order[0] == "post"
    assert kinds[calls_before_sleep - 1] == "POST", "slept right after the POST"
    assert kinds[calls_before_sleep] == "status", "and read right after the sleep"


def test_the_confirm_delay_outlasts_read_after_write_lag():
    """Seconds of lag are what GitHub has shown; the delay must leave room
    for that on both sides of the race."""
    assert reconcile.LENS_CONFIRM_DELAY_SECONDS >= 10


def test_no_delay_when_nothing_was_posted():
    api = FakeLensAPI(reviews=[lens_review()])
    run_lens_sweep(lens_gh(), api)

    assert api.slept == []
