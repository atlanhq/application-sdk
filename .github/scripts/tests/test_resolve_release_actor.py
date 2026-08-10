"""Tests for .github/scripts/resolve_release_actor.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import resolve_release_actor as rra  # noqa: E402

REPO = "atlanhq/example-app"
SHA = "a" * 40

FLEET_BOT = {"login": "atlan-app-fleet[bot]", "type": "Bot"}
HUMAN_MERGER = {"login": "example-merger", "type": "User"}
HUMAN_AUTHOR = {"login": "example-author", "type": "User"}


def make_runner(responses: dict[str, object], *, calls: list[str] | None = None):
    """Build a subprocess.run stand-in backed by a path -> payload table.

    A path mapped to ``None`` simulates a failing ``gh api`` (non-zero exit with
    an error body on stdout, which is what gh actually does); an unmapped path
    is a test bug and raises.
    """

    def runner(cmd, **_kwargs):
        assert cmd[:2] == ["gh", "api"], cmd
        path = cmd[2]
        if calls is not None:
            calls.append(path)
        if path not in responses:
            raise AssertionError(f"unexpected gh api call: {path}")
        payload = responses[path]
        if payload is None:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout='{"message": "Not Found"}',
                stderr="gh: Not Found (HTTP 404)",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    return runner


def pulls_path(sha: str = SHA) -> str:
    return f"repos/{REPO}/commits/{sha}/pulls"


def pr_path(number: int) -> str:
    return f"repos/{REPO}/pulls/{number}"


def tags_path() -> str:
    return f"repos/{REPO}/tags?per_page=100"


def compare_path(base: str = "v1.0.0", head: str = SHA) -> str:
    return f"repos/{REPO}/compare/{base}...{head}"


def commits(*logins: str, total: int | None = None) -> dict:
    """A /compare payload authored by the given logins, in order."""
    cs = [
        {"author": {"login": lg, "type": "Bot" if lg.endswith("[bot]") else "User"}}
        for lg in logins
    ]
    return {"total_commits": len(cs) if total is None else total, "commits": cs}


# ── is_bot ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "user",
    [
        None,
        {},
        {"login": "atlan-app-fleet[bot]", "type": "Bot"},
        # type alone is enough — a GitHub App without the suffix still is one.
        {"login": "some-app", "type": "Bot"},
        # login suffix alone is enough — triggering_actor arrives untyped.
        {"login": "renovate[bot]"},
    ],
)
def test_is_bot_true(user):
    assert rra.is_bot(user) is True


@pytest.mark.parametrize(
    "user",
    [
        {"login": "example-merger", "type": "User"},
        # atlan-ci is a plain user account, and GM maps it — treat it as human.
        {"login": "atlan-ci", "type": "User"},
        {"login": "example-merger"},
    ],
)
def test_is_bot_false(user):
    assert rra.is_bot(user) is False


# ── gh_api ──────────────────────────────────────────────────────────────────


def test_gh_api_returns_parsed_json():
    runner = make_runner({"users/x": {"login": "x"}})
    assert rra.gh_api("users/x", runner) == {"login": "x"}


def test_gh_api_returns_none_on_error_exit_and_ignores_error_body():
    """A non-2xx puts a JSON error body on stdout — it must not be returned."""
    runner = make_runner({"users/x": None})
    assert rra.gh_api("users/x", runner) is None


def test_gh_api_returns_none_on_non_json():
    def runner(cmd, **_kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="not json", stderr=""
        )

    assert rra.gh_api("users/x", runner) is None


def test_gh_api_returns_none_when_gh_is_missing():
    def runner(*_a, **_k):
        raise FileNotFoundError("gh")

    assert rra.gh_api("users/x", runner) is None


# ── find_pull_request ───────────────────────────────────────────────────────


def test_find_pull_request_refetches_for_merged_by():
    """The list endpoint has no merged_by, so the PR must be re-fetched."""
    calls: list[str] = []
    runner = make_runner(
        {
            pulls_path(): [{"number": 500, "user": FLEET_BOT}],
            pr_path(500): {"number": 500, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
        },
        calls=calls,
    )
    pr = rra.find_pull_request(REPO, SHA, runner)
    assert pr["merged_by"] == HUMAN_MERGER
    assert calls == [pulls_path(), pr_path(500)]


def test_find_pull_request_prefers_exact_merge_commit_match():
    runner = make_runner(
        {
            pulls_path(): [
                {"number": 1, "merge_commit_sha": "b" * 40},
                {"number": 2, "merge_commit_sha": SHA},
            ],
            pr_path(2): {"number": 2, "merged_by": HUMAN_MERGER},
        }
    )
    assert rra.find_pull_request(REPO, SHA, runner)["number"] == 2


def test_find_pull_request_falls_back_to_first_when_no_exact_match():
    runner = make_runner(
        {
            pulls_path(): [{"number": 7}, {"number": 9}],
            pr_path(7): {"number": 7, "merged_by": HUMAN_MERGER},
        }
    )
    assert rra.find_pull_request(REPO, SHA, runner)["number"] == 7


def test_find_pull_request_none_when_commit_has_no_pr():
    runner = make_runner({pulls_path(): []})
    assert rra.find_pull_request(REPO, SHA, runner) is None


def test_find_pull_request_none_when_list_call_fails():
    runner = make_runner({pulls_path(): None})
    assert rra.find_pull_request(REPO, SHA, runner) is None


def test_find_pull_request_degrades_to_list_entry_when_detail_fails():
    """A failed detail fetch still leaves the PR author usable."""
    runner = make_runner(
        {pulls_path(): [{"number": 3, "user": HUMAN_AUTHOR}], pr_path(3): None}
    )
    pr = rra.find_pull_request(REPO, SHA, runner)
    assert pr["user"] == HUMAN_AUTHOR
    assert "merged_by" not in pr


@pytest.mark.parametrize(("repo", "sha"), [("", SHA), (REPO, ""), ("", "")])
def test_find_pull_request_none_without_repo_or_sha(repo, sha):
    def runner(*_a, **_k):
        raise AssertionError("should not call the API")

    assert rra.find_pull_request(repo, sha, runner) is None


# ── resolve_actor ───────────────────────────────────────────────────────────


def test_release_event_attributes_to_merger_not_fleet_bot():
    """The bug this script exists for: bot author, bot trigger, human merger."""
    runner = make_runner(
        {
            pulls_path(): [{"number": 500, "user": FLEET_BOT}],
            pr_path(500): {"number": 500, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
        }
    )
    actor = rra.resolve_actor(REPO, SHA, "release", "atlan-app-fleet[bot]", runner)
    assert actor == "example-merger"


def test_push_event_prefers_merger_over_author():
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": HUMAN_AUTHOR}],
            pr_path(8): {"number": 8, "user": HUMAN_AUTHOR, "merged_by": HUMAN_MERGER},
        }
    )
    actor = rra.resolve_actor(REPO, SHA, "push", "example-merger", runner)
    assert actor == "example-merger"


def test_bot_merger_falls_through_to_human_author():
    """Merge-queue / auto-merge identities must not win the chain."""
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": HUMAN_AUTHOR}],
            pr_path(8): {"number": 8, "user": HUMAN_AUTHOR, "merged_by": FLEET_BOT},
        }
    )
    actor = rra.resolve_actor(REPO, SHA, "push", "atlan-app-fleet[bot]", runner)
    assert actor == "example-author"


def test_falls_back_to_triggering_actor_when_commit_has_no_pr():
    runner = make_runner({pulls_path(): []})
    actor = rra.resolve_actor(REPO, SHA, "push", "example-merger", runner)
    assert actor == "example-merger"


def test_empty_when_every_candidate_is_a_bot():
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": FLEET_BOT}],
            pr_path(8): {"number": 8, "user": FLEET_BOT, "merged_by": FLEET_BOT},
        }
    )
    assert rra.resolve_actor(REPO, SHA, "release", "atlan-app-fleet[bot]", runner) == ""


def test_workflow_dispatch_prefers_the_person_who_clicked_run():
    """A deliberate trigger outranks an unrelated older PR's merger."""
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": HUMAN_AUTHOR}],
            pr_path(8): {"number": 8, "user": HUMAN_AUTHOR, "merged_by": HUMAN_MERGER},
        }
    )
    actor = rra.resolve_actor(REPO, SHA, "workflow_dispatch", "example-clicker", runner)
    assert actor == "example-clicker"


def test_workflow_dispatch_by_a_bot_still_falls_back_to_the_merger():
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": HUMAN_AUTHOR}],
            pr_path(8): {"number": 8, "user": HUMAN_AUTHOR, "merged_by": HUMAN_MERGER},
        }
    )
    actor = rra.resolve_actor(
        REPO, SHA, "workflow_dispatch", "atlan-app-fleet[bot]", runner
    )
    assert actor == "example-merger"


# ── public_email ────────────────────────────────────────────────────────────


def test_public_email_returns_address_when_published():
    runner = make_runner({"users/example-merger": {"email": "dev@example.com"}})
    assert rra.public_email("example-merger", runner) == "dev@example.com"


def test_public_email_empty_when_private():
    runner = make_runner({"users/example-merger": {"email": None}})
    assert rra.public_email("example-merger", runner) == ""


def test_public_email_empty_when_lookup_fails():
    runner = make_runner({"users/example-merger": None})
    assert rra.public_email("example-merger", runner) == ""


def test_public_email_empty_for_empty_login():
    def runner(*_a, **_k):
        raise AssertionError("should not call the API")

    assert rra.public_email("", runner) == ""


# ── main ────────────────────────────────────────────────────────────────────


def _set_env(monkeypatch, tmp_path, **overrides):
    out = tmp_path / "gh_output"
    out.touch()
    env = {
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SHA": SHA,
        "GITHUB_EVENT_NAME": "release",
        "TRIGGERING_ACTOR": "atlan-app-fleet[bot]",
        "GITHUB_OUTPUT": str(out),
    }
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return out


def test_main_writes_login_when_email_is_private(monkeypatch, tmp_path):
    out = _set_env(monkeypatch, tmp_path)
    runner = make_runner(
        {
            pulls_path(): [{"number": 500, "user": FLEET_BOT}],
            pr_path(500): {"number": 500, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
            "users/example-merger": {"email": None},
        }
    )
    assert rra.main(runner) == 0
    assert "created_by=example-merger\n" in out.read_text()


def test_main_prefers_public_email_over_login(monkeypatch, tmp_path):
    out = _set_env(monkeypatch, tmp_path)
    runner = make_runner(
        {
            pulls_path(): [{"number": 500, "user": FLEET_BOT}],
            pr_path(500): {"number": 500, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
            "users/example-merger": {"email": "dev@example.com"},
        }
    )
    assert rra.main(runner) == 0
    assert "created_by=dev@example.com\n" in out.read_text()


def test_main_emits_empty_and_exits_zero_when_unresolvable(monkeypatch, tmp_path):
    """A release must never be blocked because attribution failed."""
    out = _set_env(monkeypatch, tmp_path)
    runner = make_runner({pulls_path(): None})
    assert rra.main(runner) == 0
    assert out.read_text() == "created_by=\nauthored_by=\n"


def test_main_exits_zero_when_resolution_raises(monkeypatch, tmp_path):
    out = _set_env(monkeypatch, tmp_path)
    runner = make_runner({pulls_path(): []})

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(rra, "resolve_actor", boom)
    assert rra.main(runner) == 0
    assert "created_by=" in out.read_text()


# ── parse_semver / previous_tag ─────────────────────────────────────────────


def test_parse_semver_accepts_v_prefix_and_bare():
    assert rra.parse_semver("v1.2.3")[:3] == (1, 2, 3)
    assert rra.parse_semver("1.2.3")[:3] == (1, 2, 3)


@pytest.mark.parametrize("tag", ["", "latest", "v1.2", "release-1", "v1.2.3.4"])
def test_parse_semver_rejects_non_semver(tag):
    assert rra.parse_semver(tag) is None


def test_prerelease_sorts_below_its_final_release():
    assert rra.parse_semver("v1.2.3-rc1") < rra.parse_semver("v1.2.3")


def test_previous_tag_sorts_by_semver_not_list_order():
    """v0.10.0 > v0.9.0 numerically but sorts lower lexically."""
    runner = make_runner(
        {
            tags_path(): [
                {"name": "v0.9.0"},
                {"name": "v0.11.0"},
                {"name": "v0.10.0"},
                {"name": "not-a-tag"},
            ]
        }
    )
    assert rra.previous_tag(REPO, "v0.11.0", runner) == "v0.10.0"


def test_previous_tag_none_when_nothing_older():
    runner = make_runner({tags_path(): [{"name": "v1.0.0"}]})
    assert rra.previous_tag(REPO, "v1.0.0", runner) is None


def test_previous_tag_none_for_unparseable_current_tag():
    def runner(*_a, **_k):
        raise AssertionError("should not call the API")

    assert rra.previous_tag(REPO, "latest", runner) is None


def test_previous_tag_none_when_tags_call_fails():
    runner = make_runner({tags_path(): None})
    assert rra.previous_tag(REPO, "v1.0.0", runner) is None


# ── resolve_contributors ────────────────────────────────────────────────────


def test_contributors_dedupe_preserving_first_appearance():
    runner = make_runner({compare_path(): commits("dev-a", "dev-b", "dev-a")})
    assert rra.resolve_contributors(REPO, "v1.0.0", SHA, set(), runner) == [
        "dev-a",
        "dev-b",
    ]


def test_contributors_exclude_the_merger():
    """The merger is already named as created_by — don't credit them twice."""
    runner = make_runner({compare_path(): commits("dev-a", "example-merger")})
    got = rra.resolve_contributors(REPO, "v1.0.0", SHA, {"example-merger"}, runner)
    assert got == ["dev-a"]


def test_contributors_drop_automation_and_bots():
    """atlan-ci authors the bump commit in every release range."""
    runner = make_runner(
        {
            compare_path(): commits(
                "atlan-ci", "atlan-app-fleet[bot]", "dev-a", "web-flow"
            )
        }
    )
    assert rra.resolve_contributors(REPO, "v1.0.0", SHA, set(), runner) == ["dev-a"]


def test_contributors_capped():
    many = [f"dev-{i}" for i in range(rra.MAX_CONTRIBUTORS + 4)]
    runner = make_runner({compare_path(): commits(*many)})
    got = rra.resolve_contributors(REPO, "v1.0.0", SHA, set(), runner)
    assert got == many[: rra.MAX_CONTRIBUTORS]


def test_contributors_empty_when_range_is_implausibly_large():
    """A huge range means the base tag is wrong — don't credit a year of history."""
    runner = make_runner(
        {compare_path(): commits("dev-a", total=rra.COMPARE_COMMIT_LIMIT + 1)}
    )
    assert rra.resolve_contributors(REPO, "v1.0.0", SHA, set(), runner) == []


def test_contributors_empty_when_compare_fails():
    runner = make_runner({compare_path(): None})
    assert rra.resolve_contributors(REPO, "v1.0.0", SHA, set(), runner) == []


def test_contributors_skip_commits_with_no_github_author():
    """A commit from an unlinked email address has author: null."""
    payload = {
        "total_commits": 2,
        "commits": [{"author": None}, {"author": {"login": "dev-a", "type": "User"}}],
    }
    runner = make_runner({compare_path(): payload})
    assert rra.resolve_contributors(REPO, "v1.0.0", SHA, set(), runner) == ["dev-a"]


# ── contributors_for_event ──────────────────────────────────────────────────


def test_release_event_credits_everyone_since_the_previous_tag():
    runner = make_runner(
        {
            tags_path(): [{"name": "v1.1.0"}, {"name": "v1.0.0"}],
            compare_path("v1.0.0"): commits("dev-a", "atlan-ci", "example-merger"),
        }
    )
    got = rra.contributors_for_event(REPO, SHA, "v1.1.0", "example-merger", runner)
    assert got == ["dev-a"]


def test_release_event_with_no_previous_tag_credits_nobody():
    runner = make_runner({tags_path(): [{"name": "v1.0.0"}]})
    got = rra.contributors_for_event(REPO, SHA, "v1.0.0", "example-merger", runner)
    assert got == []


def test_cd_push_credits_the_pr_author():
    """An untagged publish ships one merge — the range collapses to its author."""
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": HUMAN_AUTHOR}],
            pr_path(8): {"number": 8, "user": HUMAN_AUTHOR, "merged_by": HUMAN_MERGER},
        }
    )
    got = rra.contributors_for_event(REPO, SHA, "", "example-merger", runner)
    assert got == ["example-author"]


def test_cd_push_credits_nobody_when_the_pr_is_bot_authored():
    """The bump PR on the CD path — bot author, nothing to credit."""
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": FLEET_BOT}],
            pr_path(8): {"number": 8, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
        }
    )
    assert rra.contributors_for_event(REPO, SHA, "", "example-merger", runner) == []


def test_cd_push_does_not_credit_the_merger_twice():
    runner = make_runner(
        {
            pulls_path(): [{"number": 8, "user": HUMAN_MERGER}],
            pr_path(8): {"number": 8, "user": HUMAN_MERGER, "merged_by": HUMAN_MERGER},
        }
    )
    assert rra.contributors_for_event(REPO, SHA, "", "example-merger", runner) == []


# ── main: authored_by output ────────────────────────────────────────────────


def test_main_writes_authored_by(monkeypatch, tmp_path):
    out = _set_env(monkeypatch, tmp_path, RELEASE_TAG="v1.1.0")
    runner = make_runner(
        {
            pulls_path(): [{"number": 500, "user": FLEET_BOT}],
            pr_path(500): {"number": 500, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
            "users/example-merger": {"email": None},
            tags_path(): [{"name": "v1.1.0"}, {"name": "v1.0.0"}],
            compare_path("v1.0.0"): commits("dev-a", "dev-b", "example-merger"),
        }
    )
    assert rra.main(runner) == 0
    written = out.read_text()
    assert "created_by=example-merger" in written
    assert "authored_by=dev-a,dev-b" in written


def test_main_still_emits_created_by_when_contributors_fail(monkeypatch, tmp_path):
    """Contributors are a nicety — their failure must not lose the merger."""
    out = _set_env(monkeypatch, tmp_path, RELEASE_TAG="v1.1.0")
    runner = make_runner(
        {
            pulls_path(): [{"number": 500, "user": FLEET_BOT}],
            pr_path(500): {"number": 500, "user": FLEET_BOT, "merged_by": HUMAN_MERGER},
            "users/example-merger": {"email": None},
            tags_path(): None,
        }
    )
    assert rra.main(runner) == 0
    written = out.read_text()
    assert "created_by=example-merger" in written
    assert "authored_by=\n" in written
