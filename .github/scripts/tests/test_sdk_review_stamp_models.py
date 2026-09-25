"""Tests for .github/scripts/sdk_review_stamp_models.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sdk_review_stamp_models as sm  # noqa: E402  (sys.path bootstrap)

RUN = "https://github.com/o/r/actions/runs/123"
OTHER_RUN = "https://github.com/o/r/actions/runs/999"
GUESS = "**Models:** gpt-6-sol, Claude Sonnet 4.6, Claude Haiku 4.5"

BODY = (
    "<!-- SDK_REVIEW -->\n"
    "<!-- REVIEWED_HEAD: " + "a" * 40 + " -->\n"
    "## SDK Review\n\nLooks fine.\n\n---\n"
    f"{GUESS}\n"
    f"**Run:** [view workflow logs + cost]({RUN})\n"
)


def test_stamp_replaces_only_the_models_line():
    new = sm.stamp(BODY, "gpt-6-sol")
    assert new == BODY.replace(GUESS, "**Models (CLI stream observed):** gpt-6-sol")
    # Everything the approver / dedupe / verdict gate key on is untouched.
    assert "<!-- SDK_REVIEW -->" in new and "REVIEWED_HEAD" in new and RUN in new


def test_stamp_rewrites_the_current_footer_not_a_quoted_prior_footer():
    """A re-review's delta section can quote the previous summary's footer."""
    quoted = "**Models:** an older run's line\n"
    body = BODY.replace("Looks fine.\n", "Looks fine.\n" + quoted)
    new = sm.stamp(body, "gpt-6-sol")
    assert new is not None
    assert quoted in new
    assert (
        GUESS not in new
        and "**Models (CLI stream observed):** gpt-6-sol\n**Run:**" in new
    )


def test_stamp_inserts_current_footer_when_only_a_quoted_footer_exists():
    """Do not rewrite a quoted footer when this summary's footer is missing."""
    quoted = "**Models:** an older run's line\n"
    body = BODY.replace(f"{GUESS}\n", "").replace(
        "Looks fine.\n", "Looks fine.\n" + quoted
    )
    new = sm.stamp(body, "gpt-6-sol")
    assert new is not None
    assert quoted in new
    assert "**Models (CLI stream observed):** gpt-6-sol\n**Run:**" in new


def test_stamp_never_keeps_the_guess_when_the_stream_named_nothing():
    new = sm.stamp(BODY, "")
    assert new is not None
    assert f"**Models (CLI stream observed):** {sm.NOT_REPORTED}" in new
    assert "Claude" not in new


def test_stamp_inserts_the_line_above_run_when_missing():
    new = sm.stamp(BODY.replace(f"{GUESS}\n", ""), "gpt-6-sol")
    assert new is not None
    assert "**Models (CLI stream observed):** gpt-6-sol\n**Run:**" in new


def test_stamp_is_idempotent_and_leaves_footerless_bodies_alone():
    once = sm.stamp(BODY, "gpt-6-sol")
    assert once is not None
    assert sm.stamp(once, "gpt-6-sol") is None
    assert sm.stamp("<!-- SDK_REVIEW -->\nno footer", "gpt-6-sol") is None


class FakeGh:
    """Serves the comment listing and records PATCHes."""

    def __init__(self, comments, list_rc=0, patch_rc=0):
        self.comments = comments
        self.list_rc = list_rc
        self.patch_rc = patch_rc
        self.patches: list[tuple[str, str]] = []

    def __call__(self, cmd, **_):
        if "PATCH" in cmd:
            body = next(a for a in cmd if a.startswith("body="))[len("body=") :]
            self.patches.append((cmd[2], body))
            return subprocess.CompletedProcess(cmd, self.patch_rc, "", "denied")
        return subprocess.CompletedProcess(
            cmd, self.list_rc, json.dumps([self.comments]), "boom"
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("GHA_RUN_URL", RUN)
    monkeypatch.setenv("MODELS_USED", "gpt-6-sol")


def _comment(cid, body, created="2026-09-24T13:55:54Z"):
    return {"id": cid, "body": body, "created_at": created}


def _no_sleep(_):
    return None


def test_main_patches_only_this_runs_summary(env):
    theirs = BODY.replace(RUN, OTHER_RUN)
    gh = FakeGh([_comment(1, theirs), _comment(2, BODY)])
    assert sm.main(runner=gh, sleeper=_no_sleep) == 0
    assert [p[0] for p in gh.patches] == ["repos/o/r/issues/comments/2"]
    assert "**Models (CLI stream observed):** gpt-6-sol\n" in gh.patches[0][1]


def test_main_never_touches_a_summary_that_does_not_name_this_run(env):
    """No run URL ⇒ not provably ours ⇒ no write (attribute()'s rule)."""
    footerless = "<!-- SDK_REVIEW -->\n**Models:** guess\n**Run:** [logs](none)\n"
    gh = FakeGh([_comment(1, footerless)])
    assert sm.main(runner=gh, sleeper=_no_sleep) == 0
    assert gh.patches == []


def test_main_fails_open_on_listing_and_patch_errors(env):
    assert sm.main(runner=FakeGh([], list_rc=1), sleeper=_no_sleep) == 0
    gh = FakeGh([_comment(2, BODY)], patch_rc=1)
    assert sm.main(runner=gh, sleeper=_no_sleep) == 0
    assert len(gh.patches) == 1


def test_main_without_a_run_url_does_nothing(env, monkeypatch):
    monkeypatch.delenv("GHA_RUN_URL")
    gh = FakeGh([_comment(2, BODY)])
    assert sm.main(runner=gh, sleeper=_no_sleep) == 0
    assert gh.patches == []
