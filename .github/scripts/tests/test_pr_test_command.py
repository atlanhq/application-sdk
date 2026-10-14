"""Tests for .github/scripts/pr_test_command.py (FND-3839).

`/e2e` and `/gate-check` PR comments replace the `e2e` / `gate-check` labels.
What has to hold:

* only a trusted human's first-word command is honoured (``decide``);
* a request never starts the same thing twice: an active dispatched run, or a
  commit whose verdict is already ``success``, refuses unless ``force``;
* a connector branch whose ``tests.yaml`` predates `/e2e` is refused, since its
  e2e-only run would publish skipped ``tests / …`` checks over real results;
* the pending ``gate-check`` description round-trips through
  ``parse_gate_check_runs``, which is how gate_check_verdict.py learns what to
  wait for;
* the I/O posts the pending status BEFORE dispatching, and the rerun mode only
  re-runs runs (and jobs) that cannot have read the marker themselves.

The cross-file contracts — the template marker, the dispatchable workflows,
the e2e-request job name, the shared status contexts and the template's
``workflow_run`` list — are pinned at the bottom, because each lives in a
different file and drifts silently.

``gh`` is stubbed through ``GitHub``'s ``run`` seam; nothing touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import bump_pr_opt_in  # noqa: E402
import pr_test_command as ptc  # noqa: E402
import test_command_comment as sticky  # noqa: E402
from conformance.bootstrap import render as bootstrap_render  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github/workflows"
TEMPLATES = REPO_ROOT / "packages/conformance/conformance/bootstrap/templates"

REPO = "atlanhq/atlan-example-app"
SHA = "a96a27b0000000000000000000000000000000aa"
BUMP = "bump-version-main"
RUN_URL = "https://github.com/atlanhq/atlan-example-app/actions/runs/1"

#: A tests.yaml as the current template scaffolds it, reduced to what the
#: planner reads: dispatchable, e2e-only aware, and taking `head_sha`.
TESTS_YAML = (
    "on:\n  workflow_dispatch:\n    inputs:\n      run_e2e: {}\n      head_sha: {}\n"
    f"jobs:\n  tests:\n    name: ${{{{ {ptc.E2E_ONLY_MARKER} && 'e2e' || 'tests' }}}}\n"
)
DISPATCHABLE = "on:\n  workflow_dispatch:\n"


def _event(
    body: str = "/e2e",
    *,
    association: str = "MEMBER",
    user_type: str = "User",
    action: str = "created",
    on_pr: bool = True,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": 12}
    if on_pr:
        issue["pull_request"] = {"url": "x"}
    return {
        "action": action,
        "issue": issue,
        "comment": {
            "id": 99,
            "body": body,
            "author_association": association,
            "user": {"login": "a-human", "type": user_type},
        },
    }


def _request(command: str = ptc.E2E, *, force: bool = False) -> ptc.Request:
    return ptc.Request(True, command, 12, force, 99, "a-human")


def _pr(
    head_ref: str = "feat/x",
    *,
    state: str = "open",
    head_repo: str = REPO,
    behind_by: int = 0,
) -> ptc.PullRequest:
    return ptc.PullRequest(
        12, SHA, head_ref, head_repo, REPO, state, base_ref="main", behind_by=behind_by
    )


# ── decide ─────────────────────────────────────────────────────────────────


class TestDecide:
    @pytest.mark.parametrize("command", ["/e2e", "/gate-check"])
    @pytest.mark.parametrize("association", sorted(ptc.TRUSTED))
    def test_trusted_humans_are_honoured(self, command: str, association: str) -> None:
        request = ptc.decide("issue_comment", _event(command, association=association))
        assert request == ptc.Request(True, command, 12, False, 99, "a-human")

    @pytest.mark.parametrize(
        ("body", "command", "force"),
        [
            ("/E2E", "/e2e", False),
            ("  /Gate-Check  ", "/gate-check", False),
            ("/e2e force", "/e2e", True),
            ("/e2e FORCE please", "/e2e", True),
            ("/gate-check Force", "/gate-check", True),
            ("/e2e\nforce", "/e2e", True),
            ("/e2e now", "/e2e", False),
            ("/e2e forced", "/e2e", False),
        ],
    )
    def test_command_and_force_parsing(
        self, body: str, command: str, force: bool
    ) -> None:
        """The first word is the command, the second may only be `force`."""
        request = ptc.decide("issue_comment", _event(body))
        assert (request.run, request.command, request.force) == (True, command, force)

    @pytest.mark.parametrize(
        "body",
        ["", "   ", "lgtm", "please /e2e", "/e2ee", "/gate", "/e2e-only", "`/e2e`"],
    )
    def test_non_commands_are_silent(self, body: str) -> None:
        """No command → no reply: main() only comments when `command` is set."""
        request = ptc.decide("issue_comment", _event(body))
        assert request.run is False
        assert request.command == ""

    def test_null_body_is_not_a_command(self) -> None:
        event = _event()
        event["comment"]["body"] = None
        assert ptc.decide("issue_comment", event).command == ""

    @pytest.mark.parametrize("event_name", ["pull_request", "workflow_run", "push"])
    def test_other_events_are_not_commands(self, event_name: str) -> None:
        request = ptc.decide(event_name, _event())
        assert request.run is False and request.command == ""
        assert event_name in request.reason

    @pytest.mark.parametrize("action", ["edited", "deleted"])
    def test_only_new_comments(self, action: str) -> None:
        """Editing an old comment into `/e2e` must not start a run."""
        request = ptc.decide("issue_comment", _event(action=action))
        assert request.run is False and request.command == ""

    def test_issue_comments_are_not_pr_comments(self) -> None:
        request = ptc.decide("issue_comment", _event(on_pr=False))
        assert request.run is False and request.command == ""

    def test_bots_are_refused_with_a_reason(self) -> None:
        """A bot echoing `/e2e` would otherwise loop the command."""
        request = ptc.decide(
            "issue_comment", _event(user_type="Bot", association="MEMBER")
        )
        assert request.run is False
        # The command is kept, so main() replies instead of staying silent.
        assert (request.command, request.pr, request.comment_id) == ("/e2e", 12, 99)
        assert "bots" in request.reason

    @pytest.mark.parametrize(
        "association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", None]
    )
    def test_untrusted_authors_are_refused(self, association: str | None) -> None:
        request = ptc.decide("issue_comment", _event(association=association or ""))
        assert request.run is False
        assert request.command == "/e2e"
        assert request.reason == ptc.ROLE_REFUSAL


# ── plan_dispatch ──────────────────────────────────────────────────────────


def _texts(overrides: dict[str, str | None] | None = None) -> dict[str, str | None]:
    """The PR branch's workflow files: every gated one present by default."""
    texts: dict[str, str | None] = {
        "tests.yaml": TESTS_YAML,
        "checks.yml": DISPATCHABLE,
        "conformance.yaml": DISPATCHABLE,
        "vulnerability-scan.yml": DISPATCHABLE,
    }
    texts.update(overrides or {})
    return texts


class TestRefusals:
    @pytest.mark.parametrize("command", [ptc.E2E, ptc.GATE_CHECK])
    @pytest.mark.parametrize("state", ["closed", "merged"])
    def test_closed_prs(self, command: str, state: str) -> None:
        plan = ptc.plan_dispatch(
            _request(command), _pr(BUMP, state=state), {}, [], _texts()
        )
        assert plan == ptc.Plan(False, "This PR is not open.")

    @pytest.mark.parametrize("command", [ptc.E2E, ptc.GATE_CHECK])
    @pytest.mark.parametrize("head_repo", ["someone/fork", ""])
    def test_fork_prs(self, command: str, head_repo: str) -> None:
        """A deleted fork reads as an empty head repo — still not this repo."""
        plan = ptc.plan_dispatch(
            _request(command), _pr(BUMP, head_repo=head_repo), {}, [], _texts()
        )
        assert plan.act is False
        assert "fork" in plan.reply

    def test_gate_check_only_on_bump_branches(self) -> None:
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK), _pr("feat/x"), {}, [], _texts()
        )
        assert plan.act is False
        assert "bump-version" in plan.reply and "/e2e" in plan.reply

    def test_e2e_is_allowed_on_bump_branches(self) -> None:
        assert ptc.plan_dispatch(_request(), _pr(BUMP), {}, [], _texts()).act is True

    @pytest.mark.parametrize("command", [ptc.E2E, ptc.GATE_CHECK])
    def test_active_run_refuses_even_with_force(self, command: str) -> None:
        """`force` overrides "already passed", never "already running"."""
        urls = [f"https://example.invalid/runs/{i}" for i in range(6)]
        plan = ptc.plan_dispatch(
            _request(command, force=True), _pr(BUMP), {}, urls, _texts()
        )
        assert plan.act is False
        assert "already in progress" in plan.reply
        assert SHA[:7] in plan.reply
        # Bounded: a reply lists at most four runs.
        assert urls[3] in plan.reply and urls[4] not in plan.reply


class TestPlanE2e:
    def test_dispatches_tests_yaml_e2e_only_with_head_sha(self) -> None:
        plan = ptc.plan_dispatch(_request(), _pr(), {}, [], _texts())
        assert plan.act is True
        assert plan.dispatches == (
            ptc.Dispatch("tests.yaml", {"run_e2e": "only", "head_sha": SHA}),
        )
        # The `e2e` verdict is written by the run itself, not posted up front.
        assert plan.pending_status == ()
        assert SHA[:7] in plan.reply

    def test_pr_number_is_sent_only_when_declared(self) -> None:
        """An undeclared input 422s the whole dispatch."""
        declared = TESTS_YAML.replace(
            "      head_sha: {}\n", "      head_sha: {}\n      pr_number: {}\n"
        )
        plan = ptc.plan_dispatch(
            _request(), _pr(), {}, [], _texts({"tests.yaml": declared})
        )
        assert plan.dispatches[0].inputs == {
            "run_e2e": "only",
            "head_sha": SHA,
            "pr_number": "12",
        }
        plan = ptc.plan_dispatch(_request(), _pr(), {}, [], _texts())
        assert "pr_number" not in plan.dispatches[0].inputs

    def test_a_mention_of_pr_number_is_not_a_declaration(self) -> None:
        mentioned = TESTS_YAML + "# pr_number is set by test-commands\n"
        plan = ptc.plan_dispatch(
            _request(), _pr(), {}, [], _texts({"tests.yaml": mentioned})
        )
        assert "pr_number" not in plan.dispatches[0].inputs

    def test_missing_tests_yaml(self) -> None:
        plan = ptc.plan_dispatch(_request(), _pr(), {}, [], {"tests.yaml": None})
        assert plan.act is False
        assert "no `.github/workflows/tests.yaml`" in plan.reply

    def test_old_tests_yaml_is_refused_with_rebase_advice(self) -> None:
        """An old file would publish the e2e-only run as `tests / …`."""
        old = "on:\n  workflow_dispatch:\n    inputs:\n      run_e2e: {}\n"
        plan = ptc.plan_dispatch(_request(), _pr(), {}, [], {"tests.yaml": old})
        assert plan.act is False
        assert "Rebase" in plan.reply
        assert plan.dispatches == ()

    @pytest.mark.parametrize("state", ["pending", "failure", "error", None])
    def test_not_yet_green_runs(self, state: str | None) -> None:
        statuses = {} if state is None else {"e2e": state}
        assert ptc.plan_dispatch(_request(), _pr(), statuses, [], _texts()).act is True

    def test_already_green_is_skipped(self) -> None:
        plan = ptc.plan_dispatch(_request(), _pr(), {"e2e": "success"}, [], _texts())
        assert plan.act is False
        assert "already passed" in plan.reply and "/e2e force" in plan.reply

    def test_force_reruns_a_green_commit(self) -> None:
        plan = ptc.plan_dispatch(
            _request(force=True), _pr(), {"e2e": "success"}, [], _texts()
        )
        assert plan.act is True

    def test_gate_check_status_does_not_satisfy_e2e(self) -> None:
        """Each command reads its own verdict context."""
        plan = ptc.plan_dispatch(
            _request(), _pr(BUMP), {"gate-check": "success"}, [], _texts()
        )
        assert plan.act is True


class TestPlanBehindBase:
    """A dispatched run tests the branch tip, not its merge with the base."""

    def test_gate_check_behind_is_refused(self) -> None:
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK), _pr(BUMP, behind_by=2), {}, [], _texts()
        )
        assert plan.act is False
        assert "2 commit(s) behind `main`" in plan.reply
        assert "Update the branch" in plan.reply
        assert plan.dispatches == () and plan.pending_status == ()

    @pytest.mark.parametrize("force", [False, True])
    def test_gate_check_behind_wins_over_already_passed(self, force: bool) -> None:
        """Checked first: a green verdict on a stale tip still says "behind"."""
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK, force=force),
            _pr(BUMP, behind_by=1),
            {"gate-check": "success"},
            [],
            _texts(),
        )
        assert plan.act is False
        assert "behind" in plan.reply
        assert "already passed" not in plan.reply

    def test_e2e_behind_still_dispatches_with_a_note(self) -> None:
        plan = ptc.plan_dispatch(_request(), _pr(behind_by=4), {}, [], _texts())
        assert plan.act is True
        assert plan.dispatches == (
            ptc.Dispatch("tests.yaml", {"run_e2e": "only", "head_sha": SHA}),
        )
        assert "4 commit(s) behind `main`" in plan.reply

    def test_e2e_up_to_date_has_no_note(self) -> None:
        plan = ptc.plan_dispatch(_request(), _pr(), {}, [], _texts())
        assert "behind" not in plan.reply


class TestPlanGateCheck:
    def test_dispatches_every_gated_workflow_in_order(self) -> None:
        plan = ptc.plan_dispatch(_request(ptc.GATE_CHECK), _pr(BUMP), {}, [], _texts())
        assert plan.act is True
        assert plan.dispatches == (
            ptc.Dispatch("tests.yaml", {"run_e2e": "true", "head_sha": SHA}),
            ptc.Dispatch("checks.yml"),
            ptc.Dispatch("conformance.yaml"),
            ptc.Dispatch("vulnerability-scan.yml"),
        )

    def test_pending_description_round_trips(self) -> None:
        """gate_check_verdict.py learns what to wait for from this text."""
        plan = ptc.plan_dispatch(_request(ptc.GATE_CHECK), _pr(BUMP), {}, [], _texts())
        ((context, description),) = plan.pending_status
        assert context == ptc.GATE_CHECK_CONTEXT
        assert ptc.parse_gate_check_runs(description) == [
            d.workflow for d in plan.dispatches
        ]
        # GitHub.status truncates descriptions to 140 characters; the full set
        # must survive that or the verdict would wait on a subset.
        assert len(description) <= 140

    def test_missing_and_undispatchable_workflows_are_skipped(self) -> None:
        texts = _texts(
            {"checks.yml": None, "conformance.yaml": "on:\n  pull_request:\n"}
        )
        plan = ptc.plan_dispatch(_request(ptc.GATE_CHECK), _pr(BUMP), {}, [], texts)
        assert [d.workflow for d in plan.dispatches] == [
            "tests.yaml",
            "vulnerability-scan.yml",
        ]
        ((_, description),) = plan.pending_status
        assert ptc.parse_gate_check_runs(description) == [
            "tests.yaml",
            "vulnerability-scan.yml",
        ]

    def test_old_tests_yaml_gets_no_head_sha_input(self) -> None:
        """An undeclared dispatch input is a 422: the whole dispatch fails."""
        old = "on:\n  workflow_dispatch:\n    inputs:\n      run_e2e: {}\n"
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK), _pr(BUMP), {}, [], _texts({"tests.yaml": old})
        )
        assert plan.dispatches[0] == ptc.Dispatch("tests.yaml", {"run_e2e": "true"})

    def test_a_mere_mention_of_head_sha_is_not_a_declaration(self) -> None:
        """Only a declared input may be sent; a comment naming it must not
        make the planner send it and 422 the whole dispatch."""
        mentioned = (
            "on:\n  workflow_dispatch:\n    inputs:\n      run_e2e: {}\n"
            "# head_sha is not declared here\n"
        )
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK),
            _pr(BUMP),
            {},
            [],
            _texts({"tests.yaml": mentioned}),
        )
        assert plan.dispatches[0] == ptc.Dispatch("tests.yaml", {"run_e2e": "true"})

    def test_nothing_dispatchable_points_at_resync(self) -> None:
        texts = {name: None for name in ptc.GATE_CHECK_WORKFLOWS}
        plan = ptc.plan_dispatch(_request(ptc.GATE_CHECK), _pr(BUMP), {}, [], texts)
        assert plan.act is False
        assert "--resync" in plan.reply
        assert plan.pending_status == ()

    def test_already_green_is_skipped(self) -> None:
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK), _pr(BUMP), {"gate-check": "success"}, [], _texts()
        )
        assert plan.act is False
        assert "/gate-check force" in plan.reply

    def test_force_reruns_a_green_commit(self) -> None:
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK, force=True),
            _pr(BUMP),
            {"gate-check": "success"},
            [],
            _texts(),
        )
        assert plan.act is True

    @pytest.mark.parametrize("state", ["pending", "failure"])
    def test_a_previous_non_green_verdict_runs_again(self, state: str) -> None:
        plan = ptc.plan_dispatch(
            _request(ptc.GATE_CHECK), _pr(BUMP), {"gate-check": state}, [], _texts()
        )
        assert plan.act is True


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("running: tests.yaml checks.yml", ["tests.yaml", "checks.yml"]),
        ("running: ", []),
        ("passed: tests.yaml", []),
        ("", []),
        ("Running: tests.yaml", []),
    ],
)
def test_parse_gate_check_runs(description: str, expected: list[str]) -> None:
    """Only a description this script wrote is read as a run list."""
    assert ptc.parse_gate_check_runs(description) == expected


# ── plan_rerun ─────────────────────────────────────────────────────────────


class TestPlanRerun:
    def test_e2e_posts_a_success_marker_and_reruns_pr_checks(self) -> None:
        plan = ptc.plan_rerun(_request(), _pr(), {})
        assert plan.act is True
        assert plan.marker == ("e2e-request", "success", "e2e requested by @a-human")
        assert plan.workflows == ("pull_request.yaml",)

    def test_e2e_already_requested(self) -> None:
        plan = ptc.plan_rerun(_request(), _pr(), {"e2e-request": "success"})
        assert plan.act is False
        assert "/e2e force" in plan.reply

    def test_e2e_force(self) -> None:
        plan = ptc.plan_rerun(_request(force=True), _pr(), {"e2e-request": "success"})
        assert plan.act is True

    def test_gate_check_opts_in_and_reruns_the_gated_workflows(self) -> None:
        plan = ptc.plan_rerun(_request(ptc.GATE_CHECK), _pr(BUMP), {})
        assert plan.act is True
        assert plan.marker == ("gate-check", "success", "opted in by @a-human")
        assert plan.workflows == ptc.SDK_GATE_CHECK_WORKFLOWS

    def test_gate_check_already_opted_in(self) -> None:
        plan = ptc.plan_rerun(
            _request(ptc.GATE_CHECK), _pr(BUMP), {"gate-check": "success"}
        )
        assert plan.act is False
        assert "/gate-check force" in plan.reply

    def test_gate_check_force(self) -> None:
        plan = ptc.plan_rerun(
            _request(ptc.GATE_CHECK, force=True), _pr(BUMP), {"gate-check": "success"}
        )
        assert plan.act is True

    @pytest.mark.parametrize(
        ("command", "pr", "needle"),
        [
            (ptc.E2E, _pr(state="closed"), "not open"),
            (ptc.E2E, _pr(head_repo="someone/fork"), "fork"),
            (ptc.GATE_CHECK, _pr("feat/x"), "bump-version"),
        ],
    )
    def test_refusals_carry_the_reason(
        self, command: str, pr: ptc.PullRequest, needle: str
    ) -> None:
        plan = ptc.plan_rerun(_request(command), pr, {})
        assert plan.act is False
        assert needle in plan.reply
        assert plan.marker == () and plan.workflows == ()


# ── GitHub I/O ─────────────────────────────────────────────────────────────


def _iso(minute: int) -> str:
    return f"2026-10-13T12:{minute:02d}:00Z"


def _at(minute: int) -> datetime:
    return datetime(2026, 10, 13, 12, minute, tzinfo=timezone.utc)


class FakeApi:
    """Stand-in for `gh api`, routing on the REST path.

    Writes are recorded in order in ``writes`` (so tests can assert the pending
    status precedes the dispatch), and a status write is also readable back
    through both status endpoints, as on GitHub. PR comments are kept in
    ``comments``: a POST adds one, a PATCH edits it, and the listing reads them
    back, so the sticky comment's find-then-edit runs for real.
    """

    def __init__(
        self,
        *,
        pull: dict[str, Any] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        runs: dict[str, list[dict[str, Any]] | Callable[[], list[dict[str, Any]]]]
        | None = None,
        texts: dict[str, str | None] | None = None,
        jobs: dict[int, list[dict[str, Any]]] | None = None,
        fail: tuple[str, ...] = (),
        status_created_at: str = _iso(30),
        behind_by: Any = 0,
        role: str = "write",
        comments: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pull = pull or _pull_body()
        self.comments = list(comments or [])
        self.role = role
        self.behind_by = behind_by
        self.status_rows = list(statuses or [])  # newest first
        self.runs = runs or {}
        self.texts = texts or {}
        self.jobs = jobs or {}
        self.fail = fail
        self.status_created_at = status_created_at
        self.calls: list[list[str]] = []
        self.writes: list[tuple[str, dict[str, str]]] = []

    def _done(
        self, args: list[str], out: Any, code: int = 0
    ) -> subprocess.CompletedProcess:
        text = out if isinstance(out, str) else json.dumps(out)
        return subprocess.CompletedProcess(args, code, text, "boom" if code else "")

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(args)
        assert args[0] == "api"
        joined = " ".join(args)
        if any(needle in joined for needle in self.fail):
            return self._done(args, "", 1)
        if "-X" in args:
            path = args[args.index("-X") + 2]
            fields = dict(
                args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f"
            )
            self.writes.append((path, fields))
            method = args[args.index("-X") + 1]
            if method == "PATCH" and "/issues/comments/" in path:
                comment_id = int(path.rsplit("/", 1)[1])
                for row in self.comments:
                    if row["id"] == comment_id:
                        row["body"] = fields["body"]
            elif path.endswith("/comments"):
                next_id = max((c["id"] for c in self.comments), default=1000) + 1
                self.comments.append({"id": next_id, "body": fields["body"]})
            if "/statuses/" in path:
                self.status_rows.insert(
                    0,
                    {
                        "context": fields["context"],
                        "state": fields["state"],
                        "description": fields["description"],
                        "created_at": self.status_created_at,
                    },
                )
            return self._done(args, "" if path.endswith("/dispatches") else {})
        if "-H" in args:
            workflow = args[-1].split("/workflows/")[1].split("?")[0]
            text = self.texts.get(workflow)
            return self._done(args, "", 1) if text is None else self._done(args, text)
        path = next(a for a in args[1:] if a.startswith("repos/"))
        if "/pulls/" in path:
            return self._done(args, self.pull)
        if "/issues/" in path and "/comments?" in path:
            return self._done(args, [list(self.comments)])
        if "/collaborators/" in path and path.endswith("/permission"):
            push = self.role in ("admin", "maintain", "write")
            admin = self.role == "admin"
            return self._done(
                args,
                {
                    "permission": {"maintain": "write", "triage": "read"}.get(
                        self.role, self.role
                    ),
                    "role_name": self.role,
                    "user": {
                        "permissions": {"admin": admin, "push": push, "pull": True}
                    },
                },
            )
        if "/compare/" in path:
            return self._done(args, {"behind_by": self.behind_by, "ahead_by": 1})
        if "/status" in path and "/statuses" not in path:
            newest: dict[str, dict[str, Any]] = {}
            for row in self.status_rows:
                newest.setdefault(row["context"], row)
            return self._done(args, {"statuses": list(newest.values())})
        if "/statuses" in path:
            return self._done(args, self.status_rows)
        if "/jobs" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return self._paged(args, {"jobs": self.jobs.get(run_id, [])})
        if "/runs" in path:
            if "/workflows/" in path:
                workflow = path.split("/workflows/")[1].split("/")[0]
                source = self.runs.get(workflow, [])
                rows = source() if callable(source) else source
            else:
                rows = [
                    r
                    for source in self.runs.values()
                    for r in (source() if callable(source) else source)
                ]
            # Like GitHub: the listing is scoped to the queried head SHA, so a
            # run on another commit can never block or answer this one.
            if "head_sha=" in path:
                sha = path.split("head_sha=")[1].split("&")[0]
                rows = [r for r in rows if r.get("head_sha", sha) == sha]
            if "event=" in path:
                event = path.split("event=")[1].split("&")[0]
                rows = [r for r in rows if r.get("event") == event]
            return self._paged(args, {"workflow_runs": rows})
        raise AssertionError(f"unexpected gh call: {args}")

    def _paged(
        self, args: list[str], page: dict[str, Any]
    ) -> subprocess.CompletedProcess:
        """`--paginate --slurp` answers with a list of pages."""
        return self._done(args, [page] if "--paginate" in args else page)

    def dispatches(self) -> list[tuple[str, dict[str, str]]]:
        return [(p, f) for p, f in self.writes if p.endswith("/dispatches")]

    def reactions(self) -> list[str]:
        return [f["content"] for p, f in self.writes if p.endswith("/reactions")]

    def sticky(self, command: str = "e2e") -> list[dict[str, Any]]:
        """The PR comments carrying ``command``'s sticky marker."""
        mark = sticky.marker(command)
        return [c for c in self.comments if mark in c["body"]]


def _pull_body(
    head_ref: str = "feat/x", *, state: str = "open", head_repo: str | None = REPO
) -> dict[str, Any]:
    return {
        "state": state,
        "head": {
            "sha": SHA,
            "ref": head_ref,
            "repo": None if head_repo is None else {"full_name": head_repo},
        },
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }


def _run(
    run_id: int,
    *,
    status: str = "completed",
    event: str = "pull_request",
    started: int = 10,
    head_sha: str = SHA,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": status,
        "event": event,
        "head_sha": head_sha,
        "run_started_at": _iso(started),
        "html_url": f"https://example.invalid/runs/{run_id}",
    }


class TestGitHub:
    def test_pull_reads_head_and_fork_state(self) -> None:
        github = ptc.GitHub(REPO, FakeApi(pull=_pull_body(BUMP)))
        assert github.pull(12) == ptc.PullRequest(
            12, SHA, BUMP, REPO, REPO, "open", base_ref="main"
        )

    def test_behind_by_compares_base_to_head(self) -> None:
        api = FakeApi(behind_by=3)
        assert ptc.GitHub(REPO, api).behind_by("main", SHA) == 3
        assert api.calls == [["api", f"repos/{REPO}/compare/main...{SHA}"]]

    @pytest.mark.parametrize("value", [None, "3", 2.0, True])
    def test_behind_by_unknown_fails_closed(self, value: Any) -> None:
        """Unknown is not "up to date": a malformed compare must not let
        `/gate-check` run on an unestablished base (lens F-bb5064)."""
        with pytest.raises(ptc.GitHubError, match="behind_by"):
            ptc.GitHub(REPO, FakeApi(behind_by=value)).behind_by("main", SHA)

    def test_behind_by_without_a_base_ref_asks_nothing(self) -> None:
        api = FakeApi(behind_by=3)
        assert ptc.GitHub(REPO, api).behind_by("", SHA) == 0
        assert api.calls == []

    def test_a_deleted_fork_reads_as_a_fork(self) -> None:
        github = ptc.GitHub(REPO, FakeApi(pull=_pull_body(head_repo=None)))
        assert github.pull(12).is_fork is True

    def test_statuses_keep_the_newest_per_context(self) -> None:
        api = FakeApi(
            statuses=[
                {"context": "e2e", "state": "failure", "description": "new"},
                {"context": "e2e", "state": "success", "description": "old"},
                {"context": "gate-check", "state": "pending", "description": None},
            ]
        )
        assert ptc.GitHub(REPO, api).statuses(SHA) == {
            "e2e": ("failure", "new"),
            "gate-check": ("pending", ""),
        }

    def test_status_created_at_reads_the_newest_row(self) -> None:
        api = FakeApi(
            statuses=[
                {"context": "e2e", "created_at": _iso(1)},
                {"context": "gate-check", "created_at": _iso(5)},
                {"context": "gate-check", "created_at": _iso(2)},
            ]
        )
        github = ptc.GitHub(REPO, api)
        assert github.status_created_at(SHA, "gate-check") == _at(5)
        assert github.status_created_at(SHA, "e2e-request") is None

    def test_failures_raise(self) -> None:
        github = ptc.GitHub(REPO, FakeApi(fail=("/pulls/",)))
        with pytest.raises(ptc.GitHubError, match="boom"):
            github.pull(12)

    def test_unreadable_workflow_text_is_absent(self) -> None:
        github = ptc.GitHub(REPO, FakeApi(texts={}))
        assert github.workflow_text("tests.yaml", SHA) is None

    def test_dispatch_sends_ref_and_inputs(self) -> None:
        api = FakeApi()
        ptc.GitHub(REPO, api).dispatch("tests.yaml", "feat/x", {"run_e2e": "only"})
        assert api.dispatches() == [
            (
                f"repos/{REPO}/actions/workflows/tests.yaml/dispatches",
                {"ref": "feat/x", "inputs[run_e2e]": "only"},
            )
        ]

    def test_dispatch_failure_raises(self) -> None:
        github = ptc.GitHub(REPO, FakeApi(fail=("/dispatches",)))
        with pytest.raises(ptc.GitHubError, match="dispatch tests.yaml"):
            github.dispatch("tests.yaml", "feat/x", {})

    def test_status_description_is_truncated(self) -> None:
        """GitHub rejects a description over 140 characters outright."""
        api = FakeApi()
        ptc.GitHub(REPO, api).status(SHA, "e2e", "pending", "x" * 300, RUN_URL)
        assert len(api.writes[0][1]["description"]) == 140

    def test_gh_calls_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def hang(cmd: list[str], **kwargs: Any) -> None:
            assert kwargs["timeout"] == ptc.GH_TIMEOUT_S
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(ptc, "_RUNNER", hang)
        result = ptc.gh(["api", "x"])
        assert result.returncode == 124
        assert "timed out" in result.stderr


# ── run_dispatch ───────────────────────────────────────────────────────────


class TestRunDispatch:
    def test_gate_check_posts_pending_before_dispatching(self) -> None:
        """A gate must never see a dispatched run on a commit that says nothing."""
        api = FakeApi(pull=_pull_body(BUMP), texts=_texts())
        plan = ptc.run_dispatch(
            ptc.GitHub(REPO, api, dispatch_run=api), _request(ptc.GATE_CHECK), RUN_URL
        )
        assert plan.act is True
        paths = [p for p, _ in api.writes]
        assert paths[0] == f"repos/{REPO}/statuses/{SHA}"
        assert all(p.endswith("/dispatches") for p in paths[1:])
        assert len(paths) == 1 + len(ptc.GATE_CHECK_WORKFLOWS)
        status = api.writes[0][1]
        assert (status["context"], status["state"], status["target_url"]) == (
            "gate-check",
            "pending",
            RUN_URL,
        )
        # Dispatched on the branch (a dispatch needs a ref, not a SHA).
        assert {f["ref"] for _, f in api.dispatches()} == {BUMP}

    def test_e2e_dispatches_tests_yaml_only(self) -> None:
        api = FakeApi(texts=_texts())
        ptc.run_dispatch(ptc.GitHub(REPO, api), _request(), RUN_URL)
        assert api.dispatches() == [
            (
                f"repos/{REPO}/actions/workflows/tests.yaml/dispatches",
                {"ref": "feat/x", "inputs[run_e2e]": "only", "inputs[head_sha]": SHA},
            )
        ]
        # Only tests.yaml is read for /e2e, at the head commit.
        reads = [c[-1] for c in api.calls if "-H" in c]
        assert reads == [
            f"repos/{REPO}/contents/.github/workflows/tests.yaml?ref={SHA}"
        ]

    def test_an_active_run_on_another_commit_does_not_block(self) -> None:
        """Duplicate suppression is per commit: a dispatch still running for
        the previous head must not refuse a request for the new one."""
        api = FakeApi(
            texts=_texts(),
            runs={
                "tests.yaml": [
                    _run(
                        1,
                        status="in_progress",
                        event="workflow_dispatch",
                        head_sha="b" * 40,
                    )
                ]
            },
        )
        plan = ptc.run_dispatch(ptc.GitHub(REPO, api), _request(), RUN_URL)
        assert plan.act is True
        assert len(api.dispatches()) == 1

    def test_active_dispatch_run_blocks(self) -> None:
        api = FakeApi(
            texts=_texts(),
            runs={
                "tests.yaml": [
                    _run(1, status="in_progress", event="workflow_dispatch"),
                    _run(2, status="completed", event="workflow_dispatch"),
                    # A pull_request run on the commit is not a request.
                    _run(3, status="in_progress", event="pull_request"),
                ]
            },
        )
        plan = ptc.run_dispatch(ptc.GitHub(REPO, api), _request(force=True), RUN_URL)
        assert plan.act is False
        assert "runs/1" in plan.reply
        assert "runs/2" not in plan.reply and "runs/3" not in plan.reply
        assert api.writes == []

    def test_gate_check_reads_behind_by_and_refuses(self) -> None:
        api = FakeApi(pull=_pull_body(BUMP), texts=_texts(), behind_by=2)
        plan = ptc.run_dispatch(
            ptc.GitHub(REPO, api, dispatch_run=api), _request(ptc.GATE_CHECK), RUN_URL
        )
        assert plan.act is False
        assert "behind" in plan.reply
        assert ["api", f"repos/{REPO}/compare/main...{SHA}"] in api.calls
        # Refused before anything is written: no pending status, no dispatch.
        assert api.writes == []

    def test_e2e_behind_dispatches_with_a_note(self) -> None:
        api = FakeApi(texts=_texts(), behind_by=2)
        plan = ptc.run_dispatch(ptc.GitHub(REPO, api), _request(), RUN_URL)
        assert plan.act is True
        assert len(api.dispatches()) == 1
        assert "behind `main`" in plan.reply

    def test_gate_check_fails_closed_when_compare_fails(self) -> None:
        """Unknown staleness cannot vouch for what merges."""
        api = FakeApi(pull=_pull_body(BUMP), texts=_texts(), fail=("/compare/",))
        with pytest.raises(ptc.GitHubError):
            ptc.run_dispatch(
                ptc.GitHub(REPO, api, dispatch_run=api),
                _request(ptc.GATE_CHECK),
                RUN_URL,
            )
        assert api.writes == []

    def test_e2e_proceeds_when_compare_fails(self) -> None:
        """For `/e2e` "behind" is only a note, so it must not block the run."""
        api = FakeApi(texts=_texts(), fail=("/compare/",))
        plan = ptc.run_dispatch(ptc.GitHub(REPO, api), _request(), RUN_URL)
        assert plan.act is True
        assert len(api.dispatches()) == 1
        assert "behind" not in plan.reply

    def test_reads_the_newest_status_state(self) -> None:
        api = FakeApi(
            texts=_texts(),
            statuses=[{"context": "e2e", "state": "success", "description": ""}],
        )
        plan = ptc.run_dispatch(ptc.GitHub(REPO, api), _request(), RUN_URL)
        assert plan.act is False
        assert api.writes == []


# ── rerun_targets / wait_completed / run_rerun ─────────────────────────────


MARKER = _at(30)


class TestRerunTargets:
    def test_newest_pull_request_run_started_before_the_marker(self) -> None:
        api = FakeApi(
            runs={
                "sdk-gate.yaml": [
                    _run(5, started=10),
                    _run(9, started=20),
                    # A dispatch on the commit is not the PR's run.
                    _run(12, event="workflow_dispatch", started=25),
                ]
            }
        )
        targets = ptc.rerun_targets(
            ptc.GitHub(REPO, api), SHA, ("sdk-gate.yaml",), MARKER
        )
        assert targets == [ptc.RerunTarget("sdk-gate.yaml", 9, 0)]

    def test_runs_started_after_the_marker_read_it_themselves(self) -> None:
        api = FakeApi(runs={"sdk-gate.yaml": [_run(9, started=31)]})
        assert (
            ptc.rerun_targets(ptc.GitHub(REPO, api), SHA, ("sdk-gate.yaml",), MARKER)
            == []
        )

    def test_a_run_started_at_the_marker_reads_it(self) -> None:
        api = FakeApi(runs={"sdk-gate.yaml": [_run(9, started=30)]})
        assert (
            ptc.rerun_targets(ptc.GitHub(REPO, api), SHA, ("sdk-gate.yaml",), MARKER)
            == []
        )

    def test_a_queued_run_without_a_start_time_is_targeted(self) -> None:
        """No start time cannot prove it read the marker, so it is waited on."""
        run = _run(9, status="queued")
        run["run_started_at"] = None
        api = FakeApi(runs={"sdk-gate.yaml": [run]})
        assert ptc.rerun_targets(
            ptc.GitHub(REPO, api), SHA, ("sdk-gate.yaml",), MARKER
        ) == [ptc.RerunTarget("sdk-gate.yaml", 9, 0)]

    def test_workflows_with_no_runs_are_skipped(self) -> None:
        api = FakeApi(runs={"codeql.yaml": [_run(4)]})
        targets = ptc.rerun_targets(
            ptc.GitHub(REPO, api), SHA, ptc.SDK_GATE_CHECK_WORKFLOWS, MARKER
        )
        assert targets == [ptc.RerunTarget("codeql.yaml", 4, 0)]

    def _job(self, job_id: int, status: str, started: int | None) -> dict[str, Any]:
        return {
            "id": job_id,
            "name": f"PR checks / {ptc.SDK_E2E_REQUEST_JOB}",
            "status": status,
            "started_at": None if started is None else _iso(started),
        }

    def test_targets_a_completed_runs_request_job_by_id(self) -> None:
        """Re-running the job re-runs its dependents, not the whole run."""
        api = FakeApi(
            runs={"pull_request.yaml": [_run(9)]},
            jobs={9: [{"id": 1, "name": "Unit"}, self._job(77, "completed", 12)]},
        )
        targets = ptc.rerun_targets(
            ptc.GitHub(REPO, api),
            SHA,
            ("pull_request.yaml",),
            MARKER,
            ptc.SDK_E2E_REQUEST_JOB,
        )
        assert targets == [ptc.RerunTarget("pull_request.yaml", 9, 77)]

    @pytest.mark.parametrize("status", ["queued", "waiting", "pending", "requested"])
    def test_a_job_not_yet_started_reads_the_marker(self, status: str) -> None:
        api = FakeApi(
            runs={"pull_request.yaml": [_run(9, status="in_progress")]},
            jobs={9: [self._job(77, status, None)]},
        )
        assert (
            ptc.rerun_targets(
                ptc.GitHub(REPO, api),
                SHA,
                ("pull_request.yaml",),
                MARKER,
                ptc.SDK_E2E_REQUEST_JOB,
            )
            == []
        )

    def test_a_job_not_yet_created_reads_the_marker(self) -> None:
        api = FakeApi(
            runs={"pull_request.yaml": [_run(9, status="in_progress")]},
            jobs={9: [{"id": 1, "name": "Unit", "status": "in_progress"}]},
        )
        assert (
            ptc.rerun_targets(
                ptc.GitHub(REPO, api),
                SHA,
                ("pull_request.yaml",),
                MARKER,
                ptc.SDK_E2E_REQUEST_JOB,
            )
            == []
        )

    def test_a_job_started_after_the_marker_read_it(self) -> None:
        api = FakeApi(
            runs={"pull_request.yaml": [_run(9, status="in_progress")]},
            jobs={9: [self._job(77, "in_progress", 31)]},
        )
        assert (
            ptc.rerun_targets(
                ptc.GitHub(REPO, api),
                SHA,
                ("pull_request.yaml",),
                MARKER,
                ptc.SDK_E2E_REQUEST_JOB,
            )
            == []
        )

    def test_a_job_running_since_before_the_marker_is_targeted(self) -> None:
        api = FakeApi(
            runs={"pull_request.yaml": [_run(9, status="in_progress")]},
            jobs={9: [self._job(77, "in_progress", 20)]},
        )
        assert ptc.rerun_targets(
            ptc.GitHub(REPO, api),
            SHA,
            ("pull_request.yaml",),
            MARKER,
            ptc.SDK_E2E_REQUEST_JOB,
        ) == [ptc.RerunTarget("pull_request.yaml", 9, 77)]


class FakeClock:
    """Injected sleep/clock: each sleep advances the clock it reports."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now


def _completing_after(
    polls: int, run_id: int = 9
) -> Callable[[], list[dict[str, Any]]]:
    """A run listing that reports ``run_id`` in progress for ``polls`` reads."""
    seen = {"n": 0}

    def rows() -> list[dict[str, Any]]:
        seen["n"] += 1
        status = "completed" if seen["n"] > polls else "in_progress"
        return [_run(run_id, status=status, started=20)]

    return rows


class TestRunRerun:
    def _api(self, command: str = ptc.E2E, **kwargs: Any) -> FakeApi:
        head = BUMP if command == ptc.GATE_CHECK else "feat/x"
        return FakeApi(pull=_pull_body(head), **kwargs)

    def test_gate_check_marks_then_reruns_whole_runs(self) -> None:
        api = self._api(
            ptc.GATE_CHECK,
            runs={
                "sdk-gate.yaml": [_run(5)],
                "pull_request.yaml": [_run(6)],
                "codeql.yaml": [_run(7, started=40)],  # read the marker itself
            },
        )
        clock = FakeClock()
        plan = ptc.run_rerun(
            ptc.GitHub(REPO, api),
            _request(ptc.GATE_CHECK),
            RUN_URL,
            60,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert plan.act is True
        paths = [p for p, _ in api.writes]
        assert paths == [
            f"repos/{REPO}/statuses/{SHA}",
            f"repos/{REPO}/actions/runs/5/rerun",
            f"repos/{REPO}/actions/runs/6/rerun",
        ]
        assert api.writes[0][1]["state"] == "success"
        assert clock.sleeps == []

    def test_e2e_reruns_the_request_job(self) -> None:
        api = self._api(
            runs={"pull_request.yaml": [_run(9)]},
            jobs={
                9: [
                    {
                        "id": 77,
                        "name": ptc.SDK_E2E_REQUEST_JOB,
                        "status": "completed",
                        "started_at": _iso(12),
                    }
                ]
            },
        )
        clock = FakeClock()
        ptc.run_rerun(
            ptc.GitHub(REPO, api),
            _request(),
            RUN_URL,
            60,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert [p for p, _ in api.writes][1:] == [f"repos/{REPO}/actions/jobs/77/rerun"]
        assert api.writes[0][1]["context"] == "e2e-request"

    def test_waits_for_an_in_flight_run_before_rerunning(self) -> None:
        """GitHub refuses to re-run a run that is still going."""
        api = self._api(ptc.GATE_CHECK, runs={"sdk-gate.yaml": _completing_after(3)})
        clock = FakeClock()
        ptc.run_rerun(
            ptc.GitHub(REPO, api),
            _request(ptc.GATE_CHECK),
            RUN_URL,
            600,
            sleep=clock.sleep,
            clock=clock.clock,
        )
        assert clock.sleeps == [20, 20]
        assert api.writes[-1][0] == f"repos/{REPO}/actions/runs/9/rerun"

    def test_past_the_deadline_raises_without_rerunning(self) -> None:
        api = self._api(
            ptc.GATE_CHECK, runs={"sdk-gate.yaml": _completing_after(10**6)}
        )
        clock = FakeClock()
        with pytest.raises(ptc.GitHubError, match="did not finish within 60s"):
            ptc.run_rerun(
                ptc.GitHub(REPO, api),
                _request(ptc.GATE_CHECK),
                RUN_URL,
                60,
                sleep=clock.sleep,
                clock=clock.clock,
            )
        assert clock.now - 1000.0 >= 60
        assert not any(p.endswith("/rerun") for p, _ in api.writes)

    def test_marker_that_does_not_read_back_raises(self) -> None:
        """Without its timestamp, which runs predate it cannot be decided."""
        api = self._api(fail=("/statuses?per_page",))
        with pytest.raises(ptc.GitHubError):
            ptc.run_rerun(ptc.GitHub(REPO, api), _request(), RUN_URL, 60)

        api = self._api(status_created_at="")
        with pytest.raises(ptc.GitHubError, match="did not read back"):
            ptc.run_rerun(ptc.GitHub(REPO, api), _request(), RUN_URL, 60)

    def test_announces_after_the_marker_and_before_waiting(self) -> None:
        """The PR shows the request at once, not after a 30-minute wait."""
        api = self._api(ptc.GATE_CHECK, runs={"sdk-gate.yaml": _completing_after(3)})
        clock = FakeClock()
        seen: list[tuple[int, int, ptc.RerunPlan]] = []

        def announce(plan: ptc.RerunPlan) -> None:
            seen.append((len(api.writes), len(clock.sleeps), plan))

        ptc.run_rerun(
            ptc.GitHub(REPO, api),
            _request(ptc.GATE_CHECK),
            RUN_URL,
            600,
            sleep=clock.sleep,
            clock=clock.clock,
            announce=announce,
        )
        ((writes, sleeps, plan),) = seen
        assert writes == 1  # just the marker
        assert sleeps == 0
        assert clock.sleeps  # and the wait did happen afterwards
        assert plan.sha == SHA
        assert dict(plan.links) == {
            w: ptc.actions_link(REPO, w, BUMP) for w in ptc.SDK_GATE_CHECK_WORKFLOWS
        }

    def test_refusal_is_not_announced(self) -> None:
        api = self._api(statuses=[{"context": "e2e-request", "state": "success"}])
        seen: list[ptc.RerunPlan] = []
        plan = ptc.run_rerun(
            ptc.GitHub(REPO, api), _request(), RUN_URL, 60, announce=seen.append
        )
        assert seen == []
        assert plan.noop is True and plan.sha == SHA

    def test_refused_request_writes_nothing(self) -> None:
        api = self._api(statuses=[{"context": "e2e-request", "state": "success"}])
        plan = ptc.run_rerun(ptc.GitHub(REPO, api), _request(), RUN_URL, 60)
        assert plan.act is False
        assert api.writes == []


# ── main ───────────────────────────────────────────────────────────────────


@pytest.fixture
def run_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def run(
        event: dict[str, Any],
        api: FakeApi,
        mode: str = "dispatch",
        event_name: str = "issue_comment",
        extra: tuple[str, ...] = (),
        app: bool = True,
    ) -> tuple[int, dict[str, str]]:
        real = ptc.GitHub
        monkeypatch.setattr(
            ptc,
            "GitHub",
            lambda repo, dispatch_run=None: real(
                repo, api, dispatch_run=api if dispatch_run else None
            ),
        )
        # A resynced caller passes the fleet App secrets (FND-3934).
        if app:
            monkeypatch.setenv("DISPATCH_TOKEN", "app-token")
        else:
            monkeypatch.delenv("DISPATCH_TOKEN", raising=False)
        out = tmp_path / "output"
        out.write_text("")
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        path = tmp_path / "event.json"
        path.write_text(json.dumps(event))
        rc = ptc.main(
            [
                "--repo",
                REPO,
                "--event-name",
                event_name,
                "--event-path",
                str(path),
                "--mode",
                mode,
                "--run-url",
                RUN_URL,
                "--wait-seconds",
                "60",
                *extra,
            ]
        )
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
        return rc, outputs

    return run


def _comments(api: FakeApi) -> list[str]:
    return [f["body"] for p, f in api.writes if p.endswith("/comments")]


class TestDispatchIdentity:
    def test_only_the_dispatch_uses_the_app_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []

        def runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            token = (kwargs.get("env") or {}).get("GH_TOKEN", "job-token")
            seen.append((cmd[3] if len(cmd) > 3 else cmd[2], token))
            return subprocess.CompletedProcess(cmd, 0, "{}", "")

        monkeypatch.setattr(ptc, "_RUNNER", runner)
        github = ptc.GitHub(REPO, dispatch_run=ptc.gh_as("app-token"))
        github.dispatch("tests.yaml", BUMP, {})
        github.status(SHA, "gate-check", "pending", "x", RUN_URL)
        assert seen[0] == ("POST", "app-token")
        assert seen[1][1] == "job-token"


class TestMain:
    def test_non_command_is_silent(self, run_main) -> None:
        api = FakeApi()
        rc, outputs = run_main(_event("lgtm"), api)
        assert rc == 0
        assert outputs["run"] == "false"
        assert api.calls == []

    def test_refused_author_gets_a_reply(self, run_main) -> None:
        api = FakeApi()
        rc, outputs = run_main(_event(association="NONE"), api)
        assert rc == 0
        assert outputs == {"run": "false", "comment_id": "99", "pr": "12"}
        (body,) = _comments(api)
        assert body.startswith("`/e2e` ignored:")
        assert api.reactions() == ["confused"]
        # Not this command's state: the sticky comment is left alone.
        assert api.sticky() == []
        # Refused before any read: a reaction and a reply, nothing else.
        assert len(api.calls) == 2

    @pytest.mark.parametrize("role", ["read", "triage"])
    def test_read_and_triage_roles_are_refused(self, run_main, role: str) -> None:
        """`author_association` is a relationship, not a role: an org MEMBER
        with only Read or Triage passes it. The role check refuses them before
        anything is read or written."""
        api = FakeApi(texts=_texts(), role=role)
        rc, _ = run_main(_event(association="MEMBER"), api)
        assert rc == 0
        assert api.dispatches() == []
        (body,) = _comments(api)
        assert ptc.ROLE_REFUSAL in body
        assert api.reactions() == ["confused"]
        assert api.sticky() == []
        assert not any("/pulls/" in " ".join(c) for c in api.calls)

    @pytest.mark.parametrize("role", ["write", "maintain", "admin"])
    def test_write_maintain_and_admin_may_run(self, run_main, role: str) -> None:
        api = FakeApi(texts=_texts(), role=role)
        rc, _ = run_main(_event(), api)
        assert rc == 0
        assert len(api.dispatches()) == 1

    @pytest.mark.parametrize(
        ("role", "runs"),
        [("admin", True), ("maintain", False), ("write", False), ("read", False)],
    )
    def test_admin_only_gate_check(self, run_main, role: str, runs: bool) -> None:
        """application-sdk's release gate: `/gate-check` needs Admin there."""
        api = FakeApi(pull=_pull_body("bump-version-main"), role=role)
        rc, _ = run_main(
            _event("/gate-check"),
            api,
            mode="rerun",
            extra=("--gate-check-requires-admin",),
        )
        assert rc == 0
        markers = [f for p, f in api.writes if "/statuses/" in p]
        assert bool(markers) is runs
        if not runs:
            (body,) = _comments(api)
            assert ptc.ADMIN_REFUSAL in body

    def test_admin_only_flag_leaves_e2e_at_write(self, run_main) -> None:
        api = FakeApi(role="write")
        rc, _ = run_main(
            _event(), api, mode="rerun", extra=("--gate-check-requires-admin",)
        )
        assert rc == 0
        assert api.writes[0][1]["context"] == "e2e-request"

    def test_without_the_flag_gate_check_needs_only_write(self, run_main) -> None:
        """Connector repos: Write, Maintain or Admin run either command."""
        api = FakeApi(
            pull=_pull_body("bump-version-main"), texts=_texts(), role="write"
        )
        rc, _ = run_main(_event("/gate-check"), api)
        assert rc == 0
        assert api.dispatches()

    def test_sdk_workflow_passes_the_admin_flag(self) -> None:
        text = (REPO_ROOT / ".github/workflows/test-commands.yml").read_text()
        assert "--gate-check-requires-admin" in text
        reusable = (
            REPO_ROOT / ".github/workflows/test-commands-reusable.yaml"
        ).read_text()
        assert "--gate-check-requires-admin" not in reusable

    def test_an_unreadable_role_refuses(self, run_main, capsys) -> None:
        """Authorisation never fails open."""
        api = FakeApi(texts=_texts(), fail=("/collaborators/",))
        rc, _ = run_main(_event(), api)
        assert rc == 1
        assert api.dispatches() == []
        (body,) = _comments(api)
        assert "could not verify" in body

    def test_refused_plan_shows_not_started_with_the_reason(self, run_main) -> None:
        api = FakeApi(texts={"tests.yaml": "on:\n  workflow_dispatch:\n"})
        rc, outputs = run_main(_event(), api)
        assert rc == 0
        assert outputs["run"] == "true"
        assert api.dispatches() == []
        assert api.reactions() == ["confused"]
        ((comment),) = api.sticky()
        assert "🚫 not started" in comment["body"]
        assert "Rebase" in comment["body"] and RUN_URL in comment["body"]
        # The sticky is the only comment: no separate reply.
        assert len(_comments(api)) == 1

    def test_dispatch_success_shows_requested(self, run_main) -> None:
        api = FakeApi(texts=_texts())
        rc, _ = run_main(_event(), api)
        assert rc == 0
        assert len(api.dispatches()) == 1
        assert api.reactions() == ["rocket"]
        (body,) = _comments(api)
        assert body.startswith(sticky.marker("e2e"))
        assert f"⏳ requested on `{SHA[:7]}`" in body
        assert "requested by @a-human" in body
        # The Actions list of the dispatched workflow on the PR branch: a
        # dispatch returns no run id, so this is where the run is found.
        assert ptc.actions_link(REPO, "tests.yaml", "feat/x") in body

    def test_gate_check_without_the_app_is_refused(self, run_main) -> None:
        """FND-3934: dispatched as GITHUB_TOKEN, the runs would fire no
        `workflow_run` and the pending status would never settle."""
        api = FakeApi(pull=_pull_body(BUMP), texts=_texts())
        rc, _ = run_main(_event("/gate-check"), api, app=False)
        assert rc == 0
        assert api.dispatches() == []
        assert not [p for p, _ in api.writes if "/statuses/" in p]
        assert api.reactions() == ["confused"]
        ((comment),) = api.sticky("gate-check")
        assert "--resync" in comment["body"]

    def test_e2e_runs_without_the_app(self, run_main) -> None:
        """`/e2e`'s verdict comes from the run itself, not `workflow_run`."""
        api = FakeApi(texts=_texts())
        rc, _ = run_main(_event(), api, app=False)
        assert rc == 0
        assert len(api.dispatches()) == 1

    def test_an_existing_sticky_is_edited_in_place(self, run_main) -> None:
        """One comment per command: the latest request replaces the last."""
        old = {"id": 500, "body": f"{sticky.marker('e2e')}\nold verdict"}
        other = {"id": 501, "body": f"{sticky.marker('gate-check')}\nother"}
        api = FakeApi(texts=_texts(), comments=[old, other])
        rc, _ = run_main(_event(), api)
        assert rc == 0
        assert _comments(api) == []  # nothing POSTed
        patches = [p for p, _ in api.writes if p.endswith("/issues/comments/500")]
        assert patches == [f"repos/{REPO}/issues/comments/500"]
        ((comment),) = api.sticky()
        assert comment["id"] == 500 and "⏳ requested" in comment["body"]
        assert other["body"].endswith("other")  # the other command's is untouched

    def test_the_newest_sticky_is_the_one_edited(self, run_main) -> None:
        mark = sticky.marker("e2e")
        api = FakeApi(
            texts=_texts(),
            comments=[{"id": 7, "body": mark}, {"id": 9, "body": mark}],
        )
        run_main(_event(), api)
        assert [
            p
            for p, _ in api.writes
            if "/issues/comments/" in p and not p.endswith("/reactions")
        ] == [f"repos/{REPO}/issues/comments/9"]

    @pytest.mark.parametrize(
        ("statuses", "runs"),
        [
            ([{"context": "e2e", "state": "success", "description": ""}], {}),
            (
                [],
                {
                    "tests.yaml": [
                        _run(1, status="in_progress", event="workflow_dispatch")
                    ]
                },
            ),
        ],
        ids=["already-passed", "already-running"],
    )
    def test_noop_is_a_thumbs_up_and_no_comment(
        self, run_main, statuses: list, runs: dict
    ) -> None:
        """The sticky already says so; another comment would be noise."""
        api = FakeApi(texts=_texts(), statuses=statuses, runs=runs)
        rc, _ = run_main(_event(), api)
        assert rc == 0
        assert api.reactions() == ["+1"]
        assert _comments(api) == []
        assert not any(
            "/issues/comments/" in p and not p.endswith("/reactions")
            for p, _ in api.writes
        )
        assert api.dispatches() == []

    def test_rerun_mode_shows_requested(self, run_main) -> None:
        api = FakeApi()
        rc, _ = run_main(_event(), api, mode="rerun")
        assert rc == 0
        assert api.writes[0][1]["context"] == "e2e-request"
        assert api.reactions() == ["rocket"]
        (body,) = _comments(api)
        assert "⏳ requested" in body and "Requested e2e" in body

    def test_rerun_noop_is_a_thumbs_up(self, run_main) -> None:
        api = FakeApi(statuses=[{"context": "e2e-request", "state": "success"}])
        rc, _ = run_main(_event(), api, mode="rerun")
        assert rc == 0
        assert api.reactions() == ["+1"]
        assert _comments(api) == []

    def test_github_error_is_reported_and_fails(self, run_main, capsys) -> None:
        """The sticky must never be left claiming a request that did not
        start: the failure shows there as "not started", with the error."""
        api = FakeApi(texts=_texts(), fail=("/dispatches",))
        rc, _ = run_main(_event(), api)
        assert rc == 1
        assert api.reactions() == ["confused"]
        (body,) = _comments(api)
        assert "not started" in body and "Could not be started" in body
        assert "::error::" in capsys.readouterr().err

    def test_github_error_with_no_readable_pr_falls_back_to_a_reply(
        self, run_main
    ) -> None:
        # The fake fails every call naming the PR path, so the reply fails
        # too: the command must still exit 1 rather than crash.
        api = FakeApi(texts=_texts(), fail=("/pulls/",))
        rc, _ = run_main(_event(), api)
        assert rc == 1
        posts = [
            c
            for c in api.calls
            if "-X" in c and c[c.index("-X") + 2].endswith("/comments")
        ]
        assert posts and "could not be started" in " ".join(posts[0])

    def test_a_failed_gate_check_dispatch_does_not_leave_the_status_pending(
        self,
    ) -> None:
        """Release Gate would otherwise read "still running" forever."""
        api = FakeApi(pull=_pull_body(BUMP), texts=_texts(), fail=("/dispatches",))
        with pytest.raises(ptc.GitHubError):
            ptc.run_dispatch(
                ptc.GitHub(REPO, api, dispatch_run=api),
                _request(ptc.GATE_CHECK),
                RUN_URL,
            )
        states = [f["state"] for p, f in api.writes if "/statuses/" in p]
        assert states == ["pending", "error"]

    def test_a_failed_reaction_or_sticky_write_does_not_fail_the_command(
        self, run_main, capsys
    ) -> None:
        """Both are views; the dispatch is what was asked for."""
        api = FakeApi(texts=_texts(), fail=("/reactions", "/comments"))
        rc, _ = run_main(_event(), api)
        assert rc == 0
        assert len(api.dispatches()) == 1
        assert "::warning::" in capsys.readouterr().err


FLEET_APP_SECRETS = ("FLEET_APP_ID", "FLEET_APP_PRIVATE_KEY")


# ── cross-file contracts ───────────────────────────────────────────────────


def _render(name: str) -> str:
    """Bootstrap's renderer, fed this checkout's template text.

    The installed conformance package is a non-editable copy that can lag the
    source (same approach as test_bump_pr_ci_gates.py).
    """
    with mock.patch.object(
        bootstrap_render,
        "_load_template",
        lambda n: (TEMPLATES / n).read_text(encoding="utf-8"),
    ):
        return bootstrap_render.render(name)


def _on(doc: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads a bare `on:` key as boolean True.
    return doc.get("on") or doc.get(True) or {}


class TestContracts:
    def test_template_tests_yaml_carries_the_e2e_only_marker(self) -> None:
        """Without it every fresh connector would be refused `/e2e`."""
        assert ptc.E2E_ONLY_MARKER in _render("tests.yaml")

    def test_template_tests_yaml_declares_the_dispatched_inputs(self) -> None:
        """An undeclared input makes GitHub reject the whole dispatch (422)."""
        inputs = _on(yaml.safe_load(_render("tests.yaml")))["workflow_dispatch"][
            "inputs"
        ]
        assert {"run_e2e", "head_sha"} <= set(inputs)

    @pytest.mark.parametrize("workflow", ptc.GATE_CHECK_WORKFLOWS)
    def test_gate_check_workflows_are_dispatchable_templates(
        self, workflow: str
    ) -> None:
        on = _on(yaml.safe_load(_render(workflow)))
        assert "workflow_dispatch" in on, f"{workflow} cannot be dispatched"

    def test_template_test_commands_listens_to_every_gated_workflow(self) -> None:
        """`workflow_run.workflows` matches by `name:`, not file name.

        A name missing here means that workflow's completion never settles
        the `gate-check` verdict, which then stays pending forever.
        """
        on = _on(yaml.safe_load(_render("test-commands.yaml")))
        names = [yaml.safe_load(_render(w))["name"] for w in ptc.GATE_CHECK_WORKFLOWS]
        assert on["workflow_run"]["workflows"] == names

    def test_the_caller_grants_every_scope_the_reusable_uses(self) -> None:
        """A caller's `permissions:` is a ceiling: a scope a reusable job asks
        for beyond it is `none`, fleet-wide and silently."""
        rank = {"none": 0, "read": 1, "write": 2}
        caller = yaml.safe_load(_render("test-commands.yaml"))["jobs"]["commands"][
            "permissions"
        ]
        reusable = yaml.safe_load(
            (WORKFLOWS / "test-commands-reusable.yaml").read_text(encoding="utf-8")
        )
        for name, job in reusable["jobs"].items():
            for scope, level in job["permissions"].items():
                assert rank[caller.get(scope, "none")] >= rank[level], (name, scope)

    def test_the_template_passes_only_the_fleet_app_secrets(self) -> None:
        """FND-3934: without them `/gate-check` is refused; with `inherit`
        every org secret would reach a comment-triggered job."""
        job = yaml.safe_load(_render("test-commands.yaml"))["jobs"]["commands"]
        assert job["secrets"] == {
            name: f"${{{{ secrets.{name} }}}}" for name in FLEET_APP_SECRETS
        }
        reusable = yaml.safe_load(
            (WORKFLOWS / "test-commands-reusable.yaml").read_text(encoding="utf-8")
        )
        declared = _on(reusable)["workflow_call"]["secrets"]
        assert set(declared) == set(FLEET_APP_SECRETS)

    def test_the_app_token_is_scoped_and_used_only_to_dispatch(self) -> None:
        jobs = yaml.safe_load(
            (WORKFLOWS / "test-commands-reusable.yaml").read_text(encoding="utf-8")
        )["jobs"]
        steps = jobs["command"]["steps"]
        (mint,) = [s for s in steps if "create-github-app-token" in s.get("uses", "")]
        assert "/gate-check" in mint["if"]
        assert mint["with"]["repositories"] == "${{ github.event.repository.name }}"
        permissions = {
            k: v for k, v in mint["with"].items() if k.startswith("permission-")
        }
        assert permissions == {"permission-actions": "write"}
        users = [
            s["name"]
            for job in jobs.values()
            for s in job["steps"]
            if "steps.app-token" in json.dumps(s)
        ]
        assert users == ["Run the command"]

    def test_sdk_e2e_request_job_name(self) -> None:
        doc = yaml.safe_load(
            (WORKFLOWS / ptc.SDK_E2E_WORKFLOW).read_text(encoding="utf-8")
        )
        assert doc["jobs"]["e2e-request"]["name"] == ptc.SDK_E2E_REQUEST_JOB

    @pytest.mark.parametrize(
        "workflow", (*ptc.SDK_GATE_CHECK_WORKFLOWS, ptc.SDK_E2E_WORKFLOW)
    )
    def test_sdk_rerun_workflows_exist(self, workflow: str) -> None:
        assert (WORKFLOWS / workflow).is_file()

    def test_status_contexts_are_shared_with_bump_pr_opt_in(self) -> None:
        """The marker this script writes is the one bump_pr_opt_in.py reads."""
        assert ptc.E2E_REQUEST_CONTEXT == bump_pr_opt_in.E2E_REQUEST_CONTEXT
        assert ptc.GATE_CHECK_CONTEXT == bump_pr_opt_in.GATE_CHECK_CONTEXT
        assert ptc.BUMP_BRANCH_PREFIX == bump_pr_opt_in.BUMP_BRANCH_PREFIX


# ── lens round 1 ───────────────────────────────────────────────────────────


class TestDispatchInputs:
    """Dispatchability is read from the `on:` structure (lens F-795032)."""

    def test_the_scaffold_declares_its_inputs(self) -> None:
        inputs = ptc.dispatch_inputs(_render("tests.yaml"))
        assert inputs is not None
        assert {"run_e2e", "head_sha", "application_sdk_ref"} <= inputs

    @pytest.mark.parametrize(
        "name", ["checks.yml", "conformance.yaml", "vulnerability-scan.yml"]
    )
    def test_gated_shims_are_dispatchable(self, name: str) -> None:
        assert ptc.dispatch_inputs(_render(name)) == frozenset()

    def test_release_gate_is_not(self) -> None:
        assert ptc.dispatch_inputs(_render("release-gate.yaml")) is None

    @pytest.mark.parametrize(
        "text",
        [
            "on:\n  pull_request:\n# workflow_dispatch is mentioned here\njobs: {}\n",
            "on:\n  pull_request:\n    # workflow_dispatch:\njobs: {}\n",
            "name: workflow_dispatch\non:\n  push:\njobs: {}\n",
            "on:\n  pull_request:\njobs:\n  workflow_dispatch:\n    runs-on: x\n",
        ],
    )
    def test_a_mention_is_not_a_trigger(self, text: str) -> None:
        """Dispatching a workflow without the trigger 422s after the pending
        `gate-check` is posted, and the status would never settle."""
        assert ptc.dispatch_inputs(text) is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("on: [push, workflow_dispatch]\n", frozenset()),
            ("on: workflow_dispatch\n", frozenset()),
            ("'on':\n  workflow_dispatch: {}\n", frozenset()),
            (
                "on:\n  workflow_dispatch:\n    inputs:\n      a:\n        type: string\n"
                "      b: {}\n  push:\n",
                frozenset({"a", "b"}),
            ),
        ],
    )
    def test_trigger_forms(self, text: str, expected: frozenset) -> None:
        assert ptc.dispatch_inputs(text) == expected

    def test_a_workflow_mentioning_the_trigger_is_not_dispatched(self) -> None:
        texts = _texts({name: None for name in ptc.GATE_CHECK_WORKFLOWS})
        texts["checks.yml"] = "on:\n  pull_request:\n# add workflow_dispatch later\n"
        plan = ptc.plan_dispatch(_request(ptc.GATE_CHECK), _pr(BUMP), {}, [], texts)
        assert plan.act is False
        assert plan.pending_status == ()


class TestPagination:
    def test_runs_reads_every_page(self) -> None:
        """An in-flight run on page 2 must still block a duplicate
        (lens F-d8c707)."""
        pages = [
            {"workflow_runs": [_run(i, event="workflow_dispatch") for i in range(100)]},
            {
                "workflow_runs": [
                    _run(500, status="in_progress", event="workflow_dispatch")
                ]
            },
        ]

        def run(args: list[str]) -> subprocess.CompletedProcess:
            assert "--paginate" in args and "--slurp" in args
            return subprocess.CompletedProcess(args, 0, json.dumps(pages), "")

        rows = ptc.GitHub(REPO, run).runs(SHA, event="workflow_dispatch")
        assert len(rows) == 101
        assert rows[-1]["id"] == 500
