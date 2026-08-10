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
    assert out.read_text().strip() == "created_by=example-merger"


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
    assert out.read_text().strip() == "created_by=dev@example.com"


def test_main_emits_empty_and_exits_zero_when_unresolvable(monkeypatch, tmp_path):
    """A release must never be blocked because attribution failed."""
    out = _set_env(monkeypatch, tmp_path)
    runner = make_runner({pulls_path(): None})
    assert rra.main(runner) == 0
    assert out.read_text().strip() == "created_by="


def test_main_exits_zero_when_resolution_raises(monkeypatch, tmp_path):
    out = _set_env(monkeypatch, tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(rra, "resolve_actor", boom)
    assert rra.main() == 0
    assert out.read_text().strip() == "created_by="
