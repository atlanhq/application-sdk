#!/usr/bin/env python3
"""Turn a `/e2e` or `/gate-check` PR comment into exactly one test run (FND-3839).

Why comments and not labels
---------------------------
Labels used to request the expensive test legs. A `pull_request` workflow can
only see a label add by subscribing to `labeled`, and GitHub has no trigger-level
filter for WHICH label, so every bot or human label add created another run on
the same head commit. That second run's concurrency group cancelled the first
run's jobs, and the cancelled run's gate published a FAILURE on a required
context, blocking the PR (FND-48, FND-2167, FND-3832). A comment is an explicit,
one-shot request that creates no `pull_request` event at all.

What a request means
--------------------
Both commands are bound to the PR's head commit at the moment of the request,
and so is the verdict, which lands as a commit status on that SHA:

* `/e2e` — run the live-tenant e2e suite on the head commit. The verdict is the
  `e2e` status, which the Tests Gate and the Release Gate already read.
* `/gate-check` — opt a `bump-version*` PR into its full release gate (FND-3748).
  The verdict is the `gate-check` status.

A push makes a new commit with no status, so a request never outlives the code
it was made for. That replaces the old "consume the label" housekeeping.

Never run the same thing twice
------------------------------
* A commit whose verdict is already `success` is not re-run (`/e2e force` and
  `/gate-check force` override this deliberately).
* A commit with a request still in flight is not re-run: the workflow runs are
  read from the Actions API by head SHA, and the commit status is read too.
* The caller serialises requests per PR (`concurrency:` on the command
  workflow), so two comments cannot both pass the checks above at once.

Two modes, one decision
-----------------------
`dispatch` (connector repos): start the workflows on the PR branch with
`workflow_dispatch`. A `/e2e` run asks for `run_e2e: only`, which the scaffolded
`tests.yaml` reports under `e2e / …` instead of `tests / …`. That matters: GitHub
resolves a required check to the NEWEST suite on the commit and counts a skipped
job as a pass, so an e2e-only run publishing `tests / Unit` (skipped) would
override a real unit failure from the PR run. A branch whose `tests.yaml`
predates that template is refused with "rebase onto main", for the same reason.

`rerun` (application-sdk): its PR workflows cannot be dispatched without that
same collision, so the request is posted as a marker status and the runs on the
commit that started before the marker are re-run; they read the marker live.

Security
--------
The command workflow is triggered by `issue_comment`, which runs the
default-branch workflow file with secrets. It never checks out PR code: this
script reads the PR through the REST API and only dispatches or re-runs
workflows, which then run with the trust of their own event. Only commenters
with the Write, Maintain or Admin role on the repository are honoured — read
live from the collaborator-permission API, because a comment's
`author_association` is a relationship rather than a role (`MEMBER` is any org
member, even one with only Read). Bots are refused (no comment loops), and fork
PRs are refused (a fork's branch is not in this repository to dispatch on).

Kept out of the workflow YAML per docs/standards/ci.md and tested in
tests/test_pr_test_command.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable

#: A cheap first filter on the comment's ``author_association``. It is a
#: relationship, not a role — ``MEMBER`` is any org member, even one with only
#: Read here — so it is never sufficient: :meth:`GitHub.can_push` decides.
TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})

ROLE_REFUSAL = (
    "only people with the Write, Maintain or Admin role on this repository "
    "can request test runs"
)
ADMIN_REFUSAL = (
    "only people with the Admin role on this repository can request the " "release gate"
)

E2E = "/e2e"
GATE_CHECK = "/gate-check"
COMMANDS = (E2E, GATE_CHECK)
FORCE = "force"

#: Commit-status contexts. `e2e` is shared with tests-reusable.yaml
#: (e2e_commit_verdict.py, record_e2e_verdict.py) and the Release Gate.
E2E_CONTEXT = "e2e"
GATE_CHECK_CONTEXT = "gate-check"
#: The application-sdk marker for "e2e was requested on this commit".
E2E_REQUEST_CONTEXT = "e2e-request"

BUMP_BRANCH_PREFIX = "bump-version"

#: The connector `tests.yaml` marker that proves the branch reports an
#: e2e-only run under `e2e / …`. Pinned against the bootstrap template by the
#: tests, so the two cannot drift.
E2E_ONLY_MARKER = "inputs.run_e2e == 'only'"

#: Connector workflows a `/gate-check` dispatches, in order. Missing ones are
#: skipped: not every repo carries every scaffold.
TESTS_WORKFLOW = "tests.yaml"
GATE_CHECK_WORKFLOWS = (
    TESTS_WORKFLOW,
    "checks.yml",
    "conformance.yaml",
    "vulnerability-scan.yml",
)

#: application-sdk workflows a `/gate-check` re-runs (rerun mode).
SDK_GATE_CHECK_WORKFLOWS = ("sdk-gate.yaml", "pull_request.yaml", "codeql.yaml")
#: The application-sdk workflow and job a `/e2e` re-runs (rerun mode). Re-running
#: a job re-runs its dependents, i.e. the e2e legs and the connector gate.
SDK_E2E_WORKFLOW = "pull_request.yaml"
SDK_E2E_REQUEST_JOB = "Decide e2e request"

ACTIVE_RUN_STATES = frozenset(
    {"queued", "in_progress", "waiting", "requested", "pending"}
)

GH_TIMEOUT_S = 120


# ── The comment ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Request:
    run: bool
    command: str = ""
    pr: int = 0
    force: bool = False
    comment_id: int = 0
    actor: str = ""
    reason: str = ""


def decide(event_name: str, event: dict) -> Request:
    """Whether a GitHub event is an honoured test command, and which."""
    if event_name != "issue_comment":
        return Request(False, reason=f"event {event_name!r} is not a PR comment")
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    if event.get("action") != "created" or "pull_request" not in issue:
        return Request(False, reason="not a new PR comment")
    words = (comment.get("body") or "").strip().split()
    command = words[0].lower() if words else ""
    if command not in COMMANDS:
        return Request(False, reason="comment is not a test command")
    pr = int(issue["number"])
    comment_id = int(comment.get("id") or 0)
    user = comment.get("user") or {}
    actor = str(user.get("login") or "")
    if user.get("type") == "Bot":
        return Request(
            False,
            command,
            pr,
            comment_id=comment_id,
            actor=actor,
            reason="bots cannot request test runs",
        )
    if comment.get("author_association") not in TRUSTED:
        return Request(
            False,
            command,
            pr,
            comment_id=comment_id,
            actor=actor,
            reason=ROLE_REFUSAL,
        )
    force = len(words) > 1 and words[1].lower() == FORCE
    return Request(True, command, pr, force, comment_id, actor)


# ── What the repository says right now ─────────────────────────────────────


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    head_ref: str
    head_repo: str
    base_repo: str
    state: str
    base_ref: str = ""
    #: Commits on the base branch the head does not contain. A dispatched run
    #: tests the branch tip, not the PR's merge with its base as a
    #: `pull_request` run does, so a branch behind its base is tested against
    #: an older base than the one it will merge into.
    behind_by: int = 0

    @property
    def is_fork(self) -> bool:
        return self.head_repo != self.base_repo

    @property
    def is_bump(self) -> bool:
        return self.head_ref.startswith(BUMP_BRANCH_PREFIX)


@dataclass(frozen=True)
class Dispatch:
    workflow: str
    inputs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """What to do. `reply` is always posted; the rest only when `act` is set."""

    act: bool
    reply: str
    dispatches: tuple = ()
    pending_status: tuple = ()  # (context, description), posted before dispatch


def plan_dispatch(
    request: Request,
    pr: PullRequest,
    statuses: dict,
    active_runs: list,
    branch_workflows: dict,
) -> Plan:
    """Decide a connector-repo request. Pure: every fact is passed in.

    ``statuses`` maps context → the newest state on the head commit.
    ``active_runs`` lists the html_url of every not-yet-completed
    `workflow_dispatch` run on the head commit. ``branch_workflows`` maps a
    workflow file name → its text on the PR branch (None when absent).
    """
    refused = _refusal(request, pr)
    if refused:
        return refused
    sha7 = pr.head_sha[:7]

    if active_runs:
        listed = ", ".join(active_runs[:4])
        return Plan(
            False,
            f"A test run for `{sha7}` is already in progress, so this request "
            f"was not started again: {listed}",
        )

    if request.command == E2E:
        context = E2E_CONTEXT
        tests = branch_workflows.get(TESTS_WORKFLOW)
        if tests is None:
            return Plan(
                False, f"This branch has no `.github/workflows/{TESTS_WORKFLOW}`."
            )
        if E2E_ONLY_MARKER not in tests:
            return Plan(
                False,
                f"This branch's `{TESTS_WORKFLOW}` predates `/e2e` support. Rebase "
                "onto (or merge) the default branch and comment `/e2e` again. "
                "(An older file would publish the e2e-only run under the required "
                "`tests / …` checks and override this PR's real results.)",
            )
        if statuses.get(context) == "success" and not request.force:
            return Plan(
                False,
                f"e2e already passed on `{sha7}`; nothing to do. "
                "Comment `/e2e force` to run it again anyway.",
            )
        behind = (
            f" Note: this branch is {pr.behind_by} commit(s) behind `{pr.base_ref}`, "
            "and e2e tests the branch as it is, not its merge with the base; update "
            "the branch first if that matters for this change."
            if pr.behind_by
            else ""
        )
        return Plan(
            True,
            f"Started e2e on `{sha7}`. The verdict lands as the `e2e` status on "
            "that commit. A push makes it stale: comment `/e2e` again on the new head."
            + behind,
            dispatches=(
                Dispatch(TESTS_WORKFLOW, {"run_e2e": "only", "head_sha": pr.head_sha}),
            ),
        )

    # /gate-check. The release gate must test what will merge, and a dispatched
    # run tests the branch tip, so a bump branch behind its base is refused.
    # It falls behind when the bump debounce skips a push that would change
    # nothing (release_bump_debounce.py, FND-3322).
    if pr.behind_by:
        return Plan(
            False,
            f"This bump branch is {pr.behind_by} commit(s) behind `{pr.base_ref}`, so "
            "the release gate would not test what merges. Update the branch (or "
            "let the next release bump push it), then comment `/gate-check` again.",
        )
    if statuses.get(GATE_CHECK_CONTEXT) == "success" and not request.force:
        return Plan(
            False,
            f"The release gate already passed on `{sha7}`; nothing to do. "
            "Comment `/gate-check force` to run it again anyway.",
        )
    dispatches = []
    for workflow in GATE_CHECK_WORKFLOWS:
        text = branch_workflows.get(workflow)
        declared = dispatch_inputs(text) if text is not None else None
        if declared is None:
            continue
        if workflow == TESTS_WORKFLOW:
            inputs = {"run_e2e": "true"}
            if "head_sha" in declared:
                inputs["head_sha"] = pr.head_sha
            dispatches.append(Dispatch(workflow, inputs))
        else:
            dispatches.append(Dispatch(workflow))
    if not dispatches:
        return Plan(
            False,
            "None of this branch's gated workflows can be dispatched yet. Run "
            "`atlan-application-sdk-conformance bootstrap --resync` on the default "
            "branch, then rebase this PR.",
        )
    names = " ".join(d.workflow for d in dispatches)
    return Plan(
        True,
        f"Started the release gate on `{sha7}`: {', '.join(f'`{d.workflow}`' for d in dispatches)}. "
        "The verdict lands as the `gate-check` status on that commit, and Release "
        "Gate re-evaluates when it does.",
        dispatches=tuple(dispatches),
        # The aggregator (gate_check_verdict.py) reads the expected set back
        # from this description, so it is spelled as plain workflow file names.
        pending_status=((GATE_CHECK_CONTEXT, f"{GATE_CHECK_RUNS_PREFIX}{names}"),),
    )


def dispatch_inputs(text: str) -> frozenset | None:
    """The ``workflow_dispatch`` input names a workflow declares; None when it
    has no ``workflow_dispatch`` trigger at all.

    Read from the structure of the ``on:`` block, not by searching the text: a
    workflow that merely mentions the word in a comment is not dispatchable,
    and dispatching it is a 422 that would leave a pending ``gate-check`` that
    never settles. Equally, sending an undeclared input 422s the whole dispatch.
    Line-based rather than a YAML load because the command runs on a bare
    runner with no third-party packages; it understands the block form every
    scaffold uses plus the inline ``on: [a, b]`` / ``on: a`` forms.
    """
    lines = [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for index, line in enumerate(lines):
        match = re.match(r"""^["']?on["']?\s*:\s*(.*)$""", line)
        if not match:
            continue
        inline = match.group(1).split("#")[0].strip()
        if inline:
            names = {n.strip() for n in inline.strip("[]").split(",")}
            return frozenset() if "workflow_dispatch" in names else None
        return _dispatch_block(lines[index + 1 :])
    return None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _dispatch_block(lines: list) -> frozenset | None:
    """Scan the body of a block-form ``on:`` for ``workflow_dispatch``."""
    body = []
    for line in lines:
        if _indent(line) == 0:
            break
        body.append(line)
    if not body:
        return None
    trigger_indent = _indent(body[0])
    for index, line in enumerate(body):
        if _indent(line) != trigger_indent:
            continue
        if not re.match(r"^\s*workflow_dispatch\s*:", line):
            continue
        children = []
        for child in body[index + 1 :]:
            if _indent(child) <= trigger_indent:
                break
            children.append(child)
        for position, child in enumerate(children):
            if not re.match(r"^\s*inputs\s*:\s*$", child):
                continue
            under = []
            for entry in children[position + 1 :]:
                if _indent(entry) <= _indent(child):
                    break
                under.append(entry)
            if not under:
                return frozenset()
            name_indent = _indent(under[0])
            return frozenset(
                match.group(1)
                for entry in under
                if _indent(entry) == name_indent
                and (match := re.match(r"^\s*([A-Za-z0-9_-]+)\s*:", entry))
            )
        return frozenset()
    return None


#: Prefix of the pending `gate-check` description that lists the dispatched
#: workflows. gate_check_verdict.py parses it; the tests pin the round trip.
GATE_CHECK_RUNS_PREFIX = "running: "


def parse_gate_check_runs(description: str) -> list:
    """The workflow files a pending `gate-check` status is waiting on."""
    if not description.startswith(GATE_CHECK_RUNS_PREFIX):
        return []
    return description[len(GATE_CHECK_RUNS_PREFIX) :].split()


@dataclass(frozen=True)
class RerunTarget:
    """A run on the head commit that started before the request marker."""

    workflow: str
    run_id: int
    job_id: int = 0  # 0 → re-run the whole run


@dataclass(frozen=True)
class RerunPlan:
    act: bool
    reply: str
    marker: tuple = ()  # (context, state, description)
    workflows: tuple = ()  # files whose newest run on the commit must see it


def plan_rerun(request: Request, pr: PullRequest, statuses: dict) -> RerunPlan:
    """Decide an application-sdk request. Pure: every fact is passed in."""
    refused = _refusal(request, pr)
    if refused:
        return RerunPlan(False, refused.reply)
    sha7 = pr.head_sha[:7]
    if request.command == E2E:
        if statuses.get(E2E_REQUEST_CONTEXT) == "success" and not request.force:
            return RerunPlan(
                False,
                f"e2e was already requested on `{sha7}`. Comment `/e2e force` "
                "to run it again.",
            )
        return RerunPlan(
            True,
            f"Requested e2e on `{sha7}`. The connector e2e checks on that commit "
            "report the verdict.",
            marker=(
                E2E_REQUEST_CONTEXT,
                "success",
                f"e2e requested by @{request.actor}",
            ),
            workflows=(SDK_E2E_WORKFLOW,),
        )
    if statuses.get(GATE_CHECK_CONTEXT) == "success" and not request.force:
        return RerunPlan(
            False,
            f"`{sha7}` is already opted in to the release gate. Comment "
            "`/gate-check force` to re-run it.",
        )
    return RerunPlan(
        True,
        f"Opted `{sha7}` in to the release gate; its gated checks are re-running.",
        marker=(GATE_CHECK_CONTEXT, "success", f"opted in by @{request.actor}"),
        workflows=SDK_GATE_CHECK_WORKFLOWS,
    )


def _refusal(request: Request, pr: PullRequest) -> Plan | None:
    if pr.state != "open":
        return Plan(False, "This PR is not open.")
    if pr.is_fork:
        return Plan(
            False,
            "Test commands do not run on fork PRs: the branch is not in this "
            "repository, and its code must not run with this repository's secrets.",
        )
    if request.command == GATE_CHECK and not pr.is_bump:
        return Plan(
            False,
            "`/gate-check` opts a `bump-version*` PR into its release gate. "
            "For e2e on this PR, comment `/e2e`.",
        )
    return None


# ── GitHub I/O ─────────────────────────────────────────────────────────────

GhFn = Callable[[list], subprocess.CompletedProcess]


#: The process runner behind :func:`gh`. A module attribute so a test can
#: replace it here without touching the shared stdlib ``subprocess`` module.
_RUNNER: Callable[..., subprocess.CompletedProcess] = subprocess.run


def gh(args: list) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI. Bounded by GH_TIMEOUT_S."""
    try:
        return _RUNNER(
            ["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["gh", *args], 124, "", "timed out")


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo: str, run: GhFn = gh) -> None:
        self.repo = repo
        self._run = run

    def _json(self, args: list):
        result = self._run(["api", *args])
        if result.returncode != 0:
            raise GitHubError(f"gh api {' '.join(args)}: {result.stderr.strip()}")
        return json.loads(result.stdout or "null")

    def pull(self, number: int) -> PullRequest:
        body = self._json([f"repos/{self.repo}/pulls/{number}"])
        head = body["head"]
        return PullRequest(
            number=number,
            head_sha=head["sha"],
            head_ref=head["ref"],
            head_repo=(head.get("repo") or {}).get("full_name") or "",
            base_repo=body["base"]["repo"]["full_name"],
            state=body["state"],
            base_ref=body["base"].get("ref") or "",
        )

    def behind_by(self, base_ref: str, head_sha: str) -> int:
        """How many base-branch commits ``head_sha`` lacks.

        0 when there is no base ref; raises GitHubError when the comparison
        cannot be read or carries no count.
        """
        if not base_ref:
            return 0
        body = self._json([f"repos/{self.repo}/compare/{base_ref}...{head_sha}"])
        value = (body or {}).get("behind_by")
        if not isinstance(value, int) or isinstance(value, bool):
            # Unknown is not "up to date": the release gate must not run on a
            # comparison it could not establish. /e2e catches this and goes on.
            raise GitHubError("the compare response carried no integer behind_by")
        return value

    def can_push(self, login: str) -> bool:
        """Whether ``login`` holds Write, Maintain or Admin on this repository.

        ``user.permissions.push`` is true exactly for those roles, and for any
        custom role built on Write. Read and Triage are false. A response
        without the field reads as no access; a failed call raises, and the
        caller refuses — authorisation never fails open.
        """
        return self._permissions(login).get("push") is True

    def is_admin(self, login: str) -> bool:
        """Whether ``login`` holds Admin on this repository (same rules)."""
        return self._permissions(login).get("admin") is True

    def _permissions(self, login: str) -> dict:
        body = self._json([f"repos/{self.repo}/collaborators/{login}/permission"])
        return ((body or {}).get("user") or {}).get("permissions") or {}

    def statuses(self, sha: str) -> dict:
        """context → (state, description) of the NEWEST status per context."""
        body = self._json([f"repos/{self.repo}/commits/{sha}/status"])
        return {
            row["context"]: (row["state"], row.get("description") or "")
            for row in body.get("statuses") or []
        }

    def status_created_at(self, sha: str, context: str) -> datetime | None:
        rows = self._json([f"repos/{self.repo}/commits/{sha}/statuses?per_page=100"])
        for row in rows or []:  # newest first
            if row.get("context") == context:
                return _ts(row.get("created_at"))
        return None

    def runs(self, sha: str, event: str = "", workflow: str = "") -> list:
        path = (
            f"repos/{self.repo}/actions/workflows/{workflow}/runs"
            if workflow
            else f"repos/{self.repo}/actions/runs"
        )
        query = f"head_sha={sha}&per_page=100" + (f"&event={event}" if event else "")
        return self._paged(f"{path}?{query}", "workflow_runs")

    def workflow_text(self, workflow: str, ref: str) -> str | None:
        result = self._run(
            [
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{self.repo}/contents/.github/workflows/{workflow}?ref={ref}",
            ]
        )
        if result.returncode != 0:
            return None
        return result.stdout

    def run_jobs(self, run_id: int) -> list:
        return self._paged(
            f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100&filter=latest",
            "jobs",
        )

    def _paged(self, path: str, key: str) -> list:
        """Every page of a list endpoint. A decision such as "nothing is in
        flight" must not rest on whichever 100 rows came first."""
        pages = self._json(["--paginate", "--slurp", path])
        if isinstance(pages, dict):
            pages = [pages]
        rows: list = []
        for page in pages or []:
            rows.extend((page or {}).get(key) or [])
        return rows

    def post(self, path: str, fields: dict) -> None:
        args = ["-X", "POST", path]
        for key, value in fields.items():
            args += ["-f", f"{key}={value}"]
        self._json(args)

    def dispatch(self, workflow: str, ref: str, inputs: dict) -> None:
        args = [
            "-X",
            "POST",
            f"repos/{self.repo}/actions/workflows/{workflow}/dispatches",
            "-f",
            f"ref={ref}",
        ]
        for key, value in inputs.items():
            args += ["-f", f"inputs[{key}]={value}"]
        result = self._run(["api", *args])
        if result.returncode != 0:
            raise GitHubError(f"dispatch {workflow}: {result.stderr.strip()}")

    def status(
        self, sha: str, context: str, state: str, description: str, url: str
    ) -> None:
        self.post(
            f"repos/{self.repo}/statuses/{sha}",
            {
                "context": context,
                "state": state,
                "description": description[:140],
                "target_url": url,
            },
        )

    def comment(self, pr: int, body: str) -> None:
        self.post(f"repos/{self.repo}/issues/{pr}/comments", {"body": body})

    def rerun(self, run_id: int) -> None:
        self._json(["-X", "POST", f"repos/{self.repo}/actions/runs/{run_id}/rerun"])

    def rerun_job(self, job_id: int) -> None:
        self._json(["-X", "POST", f"repos/{self.repo}/actions/jobs/{job_id}/rerun"])


def _ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _notice(message: str) -> None:
    print(f"::notice::{message}", file=sys.stderr)


# ── Acting ─────────────────────────────────────────────────────────────────


def run_dispatch(github: GitHub, request: Request, run_url: str) -> Plan:
    pr = github.pull(request.pr)
    try:
        behind = github.behind_by(pr.base_ref, pr.head_sha)
    except GitHubError:
        # The release gate must test what merges, so it does not run unchecked.
        # For e2e "behind" is only a note, so it goes ahead without one.
        if request.command == GATE_CHECK:
            raise
        behind = 0
    pr = replace(pr, behind_by=behind)
    states = {k: v[0] for k, v in github.statuses(pr.head_sha).items()}
    active = [
        r.get("html_url", "")
        for r in github.runs(pr.head_sha, event="workflow_dispatch")
        if r.get("status") in ACTIVE_RUN_STATES
    ]
    wanted = (TESTS_WORKFLOW,) if request.command == E2E else GATE_CHECK_WORKFLOWS
    texts = {w: github.workflow_text(w, pr.head_sha) for w in wanted}
    plan = plan_dispatch(request, pr, states, active, texts)
    if plan.act:
        # The pending status goes up first so a gate reading it never sees a
        # window where the run exists but the commit says nothing.
        for context, description in plan.pending_status:
            github.status(pr.head_sha, context, "pending", description, run_url)
        for d in plan.dispatches:
            github.dispatch(d.workflow, pr.head_ref, d.inputs)
    return plan


def rerun_targets(
    github: GitHub,
    sha: str,
    workflows: tuple,
    marker_at: datetime,
    job_name: str = "",
) -> list:
    """The newest run per workflow on ``sha`` that started before the marker.

    A run that started after the marker reads it itself and needs nothing.
    """
    targets = []
    for workflow in workflows:
        runs = [
            r
            for r in github.runs(sha, workflow=workflow)
            if r.get("event") == "pull_request"
        ]
        if not runs:
            continue
        newest = max(runs, key=lambda r: r["id"])
        started = _ts(newest.get("run_started_at"))
        if started is not None and started >= marker_at:
            continue
        job_id = 0
        if job_name:
            job = next(
                (
                    j
                    for j in github.run_jobs(newest["id"])
                    if j.get("name", "").endswith(job_name)
                ),
                None,
            )
            if job is None:
                continue  # not created yet: it reads the marker when it runs
            if job.get("status") in ACTIVE_RUN_STATES - {"in_progress"}:
                continue  # queued: it reads the marker when it starts
            started_job = _ts(job.get("started_at"))
            if started_job is not None and started_job >= marker_at:
                continue  # already started after the marker, so it read it
            job_id = int(job["id"])
        targets.append(RerunTarget(workflow, int(newest["id"]), job_id))
    return targets


def wait_completed(
    github: GitHub,
    target: RerunTarget,
    sha: str,
    deadline: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait until ``target``'s run has completed; GitHub re-runs only those."""
    while True:
        runs = [
            r
            for r in github.runs(sha, workflow=target.workflow)
            if r["id"] == target.run_id
        ]
        if runs and runs[0].get("status") == "completed":
            return True
        if clock() >= deadline:
            return False
        sleep(20)


def run_rerun(
    github: GitHub,
    request: Request,
    run_url: str,
    wait_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RerunPlan:
    pr = github.pull(request.pr)
    states = {k: v[0] for k, v in github.statuses(pr.head_sha).items()}
    plan = plan_rerun(request, pr, states)
    if not plan.act:
        return plan
    context, state, description = plan.marker
    github.status(pr.head_sha, context, state, description, run_url)
    marker_at = github.status_created_at(pr.head_sha, context)
    if marker_at is None:
        raise GitHubError(f"the `{context}` marker did not read back on {pr.head_sha}")
    job_name = SDK_E2E_REQUEST_JOB if request.command == E2E else ""
    deadline = clock() + wait_seconds
    for target in rerun_targets(
        github, pr.head_sha, plan.workflows, marker_at, job_name
    ):
        if not wait_completed(github, target, pr.head_sha, deadline, sleep, clock):
            raise GitHubError(
                f"{target.workflow} run {target.run_id} did not finish within "
                f"{wait_seconds}s, so it could not be re-run. Re-run it by hand; it "
                "reads the marker."
            )
        if target.job_id:
            github.rerun_job(target.job_id)
        else:
            github.rerun(target.run_id)
        _notice(f"re-ran {target.workflow} run {target.run_id}")
    return plan


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event-path", required=True)
    parser.add_argument("--mode", choices=("dispatch", "rerun"), required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--wait-seconds", type=int, default=1800)
    parser.add_argument(
        "--gate-check-requires-admin",
        action="store_true",
        help="Only the Admin role may run /gate-check (application-sdk's "
        "release gate). Every other command needs Write, Maintain or Admin.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    with open(args.event_path, encoding="utf-8") as handle:
        event = json.load(handle)
    request = decide(args.event_name, event)
    _emit(run=str(request.run).lower(), comment_id=request.comment_id, pr=request.pr)
    if not request.command:
        _notice(request.reason)
        return 0

    github = GitHub(args.repo)
    if not request.run:
        github.comment(request.pr, f"`{request.command}` ignored: {request.reason}.")
        return 0
    admin_only = args.gate_check_requires_admin and request.command == GATE_CHECK
    try:
        allowed = (
            github.is_admin(request.actor)
            if admin_only
            else github.can_push(request.actor)
        )
    except GitHubError as error:
        github.comment(
            request.pr,
            f"`{request.command}` ignored: could not verify @{request.actor}'s role on "
            f"this repository, so it was not run.\n\n[command run]({args.run_url})",
        )
        print(f"::error::{error}", file=sys.stderr)
        return 1
    if not allowed:
        refusal = ADMIN_REFUSAL if admin_only else ROLE_REFUSAL
        github.comment(request.pr, f"`{request.command}` ignored: {refusal}.")
        return 0
    try:
        if args.mode == "dispatch":
            reply = run_dispatch(github, request, args.run_url).reply
        else:
            reply = run_rerun(github, request, args.run_url, args.wait_seconds).reply
    except GitHubError as error:
        github.comment(
            request.pr,
            f"`{request.command}` could not be started: {error}\n\n[command run]({args.run_url})",
        )
        print(f"::error::{error}", file=sys.stderr)
        return 1
    github.comment(request.pr, f"{reply}\n\n[command run]({args.run_url})")
    return 0


def _emit(**outputs) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    raise SystemExit(main())
