"""Tests for .github/scripts/resolve_release_actor.py.

The bug this suite exists to prevent is a *silent* one: an attribution lookup
that reads a field the API never populates returns nobody, falls back, and goes
green. Nothing in a workflow run distinguishes that from a working lookup — so
the shape of the real API responses is pinned here as fixtures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import resolve_release_actor as actor  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = _REPO_ROOT / ".github/workflows/build-and-publish-app.yaml"

REPO = "atlanhq/application-sdk"
SHA = "a657de1a7a46c419cbb6a99682ca98c29bd7916a"

# Trimmed from the live response for application-sdk#3240 (a bot-authored PR
# merged by a human). `merged_by` is present and null: the list endpoint does
# not populate it, which is exactly why the second hop exists.
COMMIT_PULLS_RESPONSE = [
    {
        "number": 3240,
        "state": "closed",
        "merged_at": "2026-08-17T23:17:23Z",
        "merged_by": None,
        "user": {"login": "atlan-app-fleet[bot]"},
    }
]

# The same PR from the single-PR endpoint, which does populate `merged_by`.
SINGLE_PULL_RESPONSE = {
    "number": 3240,
    "merged_at": "2026-08-17T23:17:23Z",
    "merged_by": {"login": "a-human"},
    "user": {"login": "atlan-app-fleet[bot]"},
}


def fake_gh(responses: dict, calls: list | None = None):
    """A ``_run_gh`` stub serving *responses* keyed by API path.

    A path with no entry returns "" — the shape of any ``gh`` failure.
    """

    def run(args: list) -> str:
        path = args[1]
        if calls is not None:
            calls.append(path)
        payload = responses.get(path)
        return "" if payload is None else json.dumps(payload)

    return run


class TestPushAttribution:
    def test_attributes_to_the_merger_not_the_bot_author(self):
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
            }
        )
        assert actor.resolve_actor("push", REPO, SHA, "who-pushed", run) == "a-human"

    def test_the_merger_is_read_from_the_single_pr_endpoint(self):
        """The list response alone must not be treated as an answer.

        Serving *only* the list endpoint — with its null ``merged_by`` — has to
        fall back. A reader that mistakes the list for the source of truth would
        return the fallback here too, so the call trace is asserted as well:
        both hops must actually be made.
        """
        calls: list = []
        run = fake_gh(
            {f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE}, calls
        )
        assert actor.resolve_actor("push", REPO, SHA, "who-pushed", run) == "who-pushed"
        assert calls == [
            f"repos/{REPO}/commits/{SHA}/pulls",
            f"repos/{REPO}/pulls/3240",
        ]

    def test_falls_back_when_the_sha_has_no_pull_request(self):
        # A direct push to main: the pusher is the author of the change.
        run = fake_gh({f"repos/{REPO}/commits/{SHA}/pulls": []})
        assert actor.resolve_actor("push", REPO, SHA, "who-pushed", run) == "who-pushed"

    def test_falls_back_when_the_lookup_fails(self):
        # Every `gh` failure — no token, 404, rate limit — arrives as "".
        run = fake_gh({})
        assert actor.resolve_actor("push", REPO, SHA, "who-pushed", run) == "who-pushed"

    def test_falls_back_when_the_pr_is_not_merged(self):
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": {"number": 3240, "merged_by": None},
            }
        )
        assert actor.resolve_actor("push", REPO, SHA, "who-pushed", run) == "who-pushed"

    def test_ignores_an_error_body_gh_wrote_to_stdout(self):
        """``gh`` prints its JSON error body to stdout, so parseable is not sane.

        ``_run_gh`` gates on the exit code; this pins the layer above it — a
        dict where a list belongs must not be read as a PR.
        """
        run = fake_gh({f"repos/{REPO}/commits/{SHA}/pulls": {"message": "Not Found"}})
        assert actor.resolve_actor("push", REPO, SHA, "who-pushed", run) == "who-pushed"


class TestNonPushAttribution:
    @pytest.mark.parametrize("event", ["workflow_dispatch", "schedule"])
    def test_attributes_to_whoever_triggered_the_run(self, event: str):
        """A deliberate trigger names its own owner — no lookup, no fallback."""
        calls: list = []
        run = fake_gh({}, calls)
        assert (
            actor.resolve_actor(event, REPO, SHA, "who-dispatched", run)
            == "who-dispatched"
        )
        assert calls == []


class TestReleaseAttribution:
    """`release: published` is never triggered by a human.

    The tag is cut by tag-and-release when a release-labelled PR merges, so the
    Release is authored by the Fleet App bot and the publish run's triggering
    actor is that bot. Trusting it attributed every tagged release to
    `atlan-app-fleet`. GITHUB_SHA is the tagged commit — the merge commit of
    that PR — so the same two hops recover the human who merged it.
    """

    def test_attributes_to_the_merger_not_the_publishing_bot(self):
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
            }
        )
        assert (
            actor.resolve_actor("release", REPO, SHA, "atlan-app-fleet[bot]", run)
            == "a-human"
        )

    def test_falls_back_to_the_bot_rather_than_failing_the_publish(self):
        """A tag pushed by hand has no PR. Degrading beats blocking a release."""
        run = fake_gh({f"repos/{REPO}/commits/{SHA}/pulls": []})
        assert (
            actor.resolve_actor("release", REPO, SHA, "atlan-app-fleet[bot]", run)
            == "atlan-app-fleet[bot]"
        )


class TestReleaseTagSignal:
    """The tag identifies a tagged release without consulting the event name.

    ``GITHUB_EVENT_NAME`` in a called reusable workflow is the caller's event
    (``release``), not ``workflow_call`` — see the module docstring for the
    reproducer. The tag is carried anyway so attribution does not rest on that
    one behaviour: every caller sets ``release_tag`` only for a release event,
    so a non-empty tag means the same thing on its own.
    """

    def test_a_tag_alone_reaches_the_merger(self):
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
            }
        )
        assert (
            actor.resolve_actor(
                "workflow_call",
                REPO,
                SHA,
                "atlan-app-fleet[bot]",
                run,
                release_tag="v0.3.2",
            )
            == "a-human"
        )

    def test_a_blank_tag_is_not_a_release(self):
        """Whitespace is what an unset workflow input degrades to, not a tag."""
        calls: list = []
        run = fake_gh({}, calls)
        assert (
            actor.resolve_actor(
                "workflow_dispatch", REPO, SHA, "who-dispatched", run, release_tag=" \n"
            )
            == "who-dispatched"
        )
        assert calls == []

    def test_a_tagless_push_is_still_pr_derived(self):
        """Deploy-on-merge apps publish on `push` and never set a tag."""
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
            }
        )
        assert (
            actor.resolve_actor("push", REPO, SHA, "who-pushed", run, release_tag="")
            == "a-human"
        )


class TestCreatedByValue:
    def test_prefers_a_public_email_over_the_login(self):
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
                "users/a-human": {"login": "a-human", "email": "a-human@example.com"},
            }
        )
        assert (
            actor.resolve("push", REPO, SHA, "who-pushed", run) == "a-human@example.com"
        )

    def test_falls_back_to_the_login_when_no_email_is_public(self):
        # The common case: GitHub returns `"email": null` for most accounts.
        run = fake_gh(
            {
                f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
                "users/a-human": {"login": "a-human", "email": None},
            }
        )
        assert actor.resolve("push", REPO, SHA, "who-pushed", run) == "a-human"


class TestStdoutContract:
    def test_writes_only_a_key_value_line_to_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ):
        """The caller redirects stdout into ``$GITHUB_OUTPUT``.

        The runner fails the step on any line there without an ``=``, so
        diagnostics must go to stderr.
        """
        monkeypatch.setattr(
            actor,
            "_run_gh",
            fake_gh(
                {
                    f"repos/{REPO}/commits/{SHA}/pulls": COMMIT_PULLS_RESPONSE,
                    f"repos/{REPO}/pulls/3240": SINGLE_PULL_RESPONSE,
                }
            ),
        )
        rc = actor.main(
            [
                "--event-name",
                "push",
                "--repo",
                REPO,
                "--sha",
                SHA,
                "--triggering-actor",
                "who-pushed",
            ]
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == "created_by=a-human\n"


class TestWorkflowWiring:
    """The step that invokes this script, asserted rather than trusted.

    Both failures guarded here are invisible at runtime: an unauthenticated
    ``gh`` exits non-zero and the resolver degrades to the triggering actor, and
    a ``created_by`` wired to a stale step id renders as an empty string.
    """

    @property
    def steps(self) -> list:
        workflow = yaml.safe_load(WORKFLOW.read_text())
        return workflow["jobs"]["publish"]["steps"]

    def _step(self, name: str) -> dict:
        for step in self.steps:
            if step.get("name") == name:
                return step
        raise AssertionError(f"publish job has no step named {name!r}")

    def test_the_resolver_step_is_given_a_github_token(self):
        assert "GH_TOKEN" in self._step("Resolve release attribution")["env"]

    def test_the_publish_step_reads_the_resolver_output(self):
        resolver = self._step("Resolve release attribution")
        publish = self._step("Publish version")
        assert publish["env"]["CREATED_BY"] == (
            "${{ steps." + resolver["id"] + ".outputs.created_by }}"
        )

    def test_the_resolver_step_is_given_the_release_tag(self):
        """Without this the tag signal is dead and only the event name is left.

        Both halves are asserted: an env var carrying ``inputs.release_tag``,
        and a command line that actually passes it.
        """
        step = self._step("Resolve release attribution")
        assert step["env"]["RELEASE_TAG"] == "${{ inputs.release_tag }}"
        assert '--release-tag "${RELEASE_TAG}"' in step["run"]

    def test_the_publish_step_no_longer_resolves_the_actor_inline(self):
        # The inline lookup this replaced was dead for two independent reasons
        # (no token, unpopulated field) and neither was detectable from a run.
        assert "gh api" not in self._step("Publish version")["run"]
