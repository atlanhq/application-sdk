"""Decide from a GitHub event whether connector-reviewer runs, on which PR, and whether forced.

Kept out of the workflow YAML (docs/standards/ci.md: no branching shell in
`run:` blocks) and tested here instead.

connector-reviewer reviews only when asked — a push never spends money on its own:

- `issue_comment` on a PR whose body starts with `/review`, from someone who may
  ask (see "Who may ask"): run. Each run continues from the last one (it
  reviews only commits since the last reviewed head). `/review force` also
  bypasses the unchanged-head and round-cap admission rules and redoes the
  approach check (the $ cap still holds — force cannot buy more budget).
  `/review verify-budget` (or `/review force verify-budget`) gives the verify call
  its larger output budget for that run; the run itself says when it is worth it.
- `/review dismiss F-1a2b3c [F-…] <reason>`: close findings the team decided not to
  fix, with the reason on record. No model call. A blocking (critical/high) finding
  cannot be dismissed by the PR's own author: someone else has to agree.
- `/review recheck [verify-budget] F-1a2b3c [F-…] [reason]`: re-judge open findings with repo-aware
  evidence the line review never had (does the file/symbol exist on the merge
  base, who calls it; in a workflow or action file, the GitHub API visibility
  of each repository a step checks out with `repository: owner/name`, at most
  three per finding).
  Anyone who may trigger connector-reviewer may ask, the PR author included: this is how an
  author gets a disputed blocking finding a second look without a human. The
  finding is resolved with the evidence on record, or kept and the evidence
  shown. A compatibility claim on code absent from the merge base resolves with
  no model call; anything else costs one call from the PR's $ cap. It does not
  count as a round (`max_rounds` does not stop it). The reason is the author's
  claim: the model checks it against the evidence, never follows it.
  `verify-budget` right after `recheck` gives that call verify's larger output
  budget, as on a round; a re-check that runs out says so and names it.
- `/review waive <reason>`: set the reviewer's verdict aside for the current head
  commit, with who and why on record. No model call. Anyone who may ask may
  waive, the PR author included; the reason is required.
- Any other word after `/review` (a typo of `force`, a command that does not
  exist) gets a usage reply and starts nothing: a full review costs money, and
  a mistyped command must not buy one.
- `workflow_dispatch` with a PR number: run (maintainers, from the Actions tab).
- Anything else, including every `pull_request*` event: do not run.

Who may ask
-----------
lens trusts a comment's `author_association`. That is a relationship, not a
role: `MEMBER` is any member of the organisation, even one with only Read on
this repository. Here the commenter's LIVE permission on the repository
decides, read from the collaborator-permission API (`can_push`): Write,
Maintain or Admin. `.github/scripts/pr_test_command.py` does the same for
`/e2e` and `/gate-check`, for the same reason. A failed lookup is a refusal:
authorisation never fails open.

A bot is honoured only when it is a trusted GitHub App: the comment carries
`performed_via_github_app.slug` naming an App on the `trusted_apps` list AND
its author is that App's bot account, `<slug>[bot]`. GitHub sets both from the
token that posted the comment, so a commenter cannot forge either. A trusted
App has the rights of a person with Write. Every other bot is refused without
a reply, so two bots can never answer each other in a loop. The reviewer's
own comments never start with `/review` (`github.safe_body`), so it cannot
trigger itself even though its App is on the list.

A pull request from a fork is refused (`is_fork`); the CLI checks that once it
has read the pull request.
"""

from __future__ import annotations

import http.client
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .github import GitHubError
from .typed import EMPTY

COMMAND = "/review"
REASON_KEEP = 300


@dataclass
class Decision:
    run: bool
    pr: int = 0
    force: bool = False
    reason: str = ""
    comment_id: int = 0  # the `/review` comment to react on (0 = none, e.g. a dispatch)
    dismiss: list[str] = field(
        default_factory=list
    )  # finding ids to close, for `/review dismiss`
    dismiss_reason: str = ""
    actor: str = ""  # who asked
    pr_author: str = ""
    verify_budget: bool = False  # `/review verify-budget`: the raised verify budget
    recheck: list[str] = field(
        default_factory=list
    )  # finding ids to re-judge, for `/review recheck`
    recheck_reason: str = ""
    waive: bool = False  # `/review waive <reason>`
    waive_reason: str = ""
    # A refusal worth telling the commenter about (a usage error, no access).
    # False for a refusal that must stay silent: a bot, or a comment not for us.
    reply: bool = False


_FINDING_ID = re.compile(r"^F-[0-9a-f]{6}$")
_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VERIFY_BUDGET = "verify-budget"
DISMISS_USAGE = "usage: `/review dismiss F-1a2b3c [F-…] <reason>` — at least one finding id and a reason"
RECHECK_USAGE = "usage: `/review recheck [verify-budget] F-1a2b3c [F-…] [reason]` — at least one finding id"
WAIVE_USAGE = "usage: `/review waive <reason>` — a reason is required"
REVIEW_USAGE = (
    "usage: `/review [force] [verify-budget]` to review, or "
    "`/review dismiss|recheck|waive …` — that command is not one of these, "
    "so nothing was started"
)
USAGE_ERRORS = (DISMISS_USAGE, RECHECK_USAGE, WAIVE_USAGE, REVIEW_USAGE)
# What may follow `/review` in a plain review request.
REVIEW_OPTIONS = ("force", VERIFY_BUDGET)
ROLE_REFUSAL = (
    "only people with the Write, Maintain or Admin role on this repository "
    "can ask for a review"
)
ROLE_UNKNOWN = (
    "your access to this repository could not be confirmed, so the request "
    "was not run; comment again in a moment"
)
BOT_REFUSAL = "this bot cannot ask for a review"
FORK_REFUSAL = (
    "pull requests from a fork are not reviewed: the branch is not in this repository"
)


def plain(text: str) -> str:
    """A commenter's reason as it is stored and shown: one line that cannot
    open an HTML comment or a tag in the reviewer's own summary."""
    return " ".join(text.replace("<", "‹").replace(">", "›").split())


def addressed(event_name: str, event: dict[str, Any]) -> bool:
    """False only for a comment event that is plainly not a `/review` request, so
    the CLI can stop before it reads its config or calls GitHub. Every other
    event goes on to `decide`."""
    if event_name != "issue_comment":
        return True
    issue = event.get("issue") or EMPTY
    comment = event.get("comment") or EMPTY
    words = str(comment.get("body") or "").strip().split()
    return (
        event.get("action") == "created"
        and "pull_request" in issue
        and bool(words)
        and words[0].lower() == COMMAND
    )


def is_fork(pr: dict[str, Any], repo: str) -> bool:
    """Whether the pull request's head branch lives in another repository. A
    head whose repository cannot be read (deleted fork) counts as a fork."""
    head_repo = ((pr.get("head") or EMPTY).get("repo") or EMPTY).get("full_name")
    base_repo = ((pr.get("base") or EMPTY).get("repo") or EMPTY).get("full_name")
    return str(head_repo or "") != str(base_repo or repo)


def trusted_app(comment: Any, trusted_apps: Sequence[str]) -> str:
    """The slug of the trusted App that posted `comment`, or "".

    Both halves must hold: GitHub attributes the comment to an App on the list,
    and the comment's author is that App's bot account. Fails closed on a
    missing attribution, a malformed slug, or any mismatch."""
    app = (comment.get("performed_via_github_app") or EMPTY).get("slug")
    user = comment.get("user") or EMPTY
    if (
        isinstance(app, str)
        and _APP_SLUG.match(app)
        and app in trusted_apps
        and user.get("type") == "Bot"
        and user.get("login") == f"{app}[bot]"
    ):
        return app
    return ""


def _may_ask(
    comment: Any,
    trusted_apps: Sequence[str],
    can_push: Callable[[str], bool] | None,
) -> tuple[bool, str, bool]:
    """(allowed, why not, whether the refusal gets a reply)."""
    user = comment.get("user") or EMPTY
    login = str(user.get("login") or "")
    if user.get("type") == "Bot":
        return bool(trusted_app(comment, trusted_apps)), BOT_REFUSAL, False
    if not login or can_push is None:
        return False, ROLE_REFUSAL, True
    try:
        return bool(can_push(login)), ROLE_REFUSAL, True
    except (GitHubError, OSError, ValueError, http.client.HTTPException):
        # Fail closed on any failed lookup: an HTTP error (GitHubError), a
        # network error that urlopen raises as such (DNS, a reset, a timeout:
        # URLError and TimeoutError are OSError), a body that is not JSON
        # (ValueError) or a reply cut short (http.client.IncompleteRead). Not
        # authorised, with a reply that says why, never a crash.
        return False, ROLE_UNKNOWN, True


def decide(
    event_name: str,
    event: dict[str, Any],
    repo: str,
    *,
    trusted_apps: Sequence[str] = (),
    can_push: Callable[[str], bool] | None = None,
) -> Decision:
    """`can_push(login)` answers whether a person holds Write, Maintain or
    Admin on the repository (`GitHub.can_push`); without it no person may ask.
    `trusted_apps` are the App slugs whose bot comments are honoured."""
    if event_name in ("pull_request", "pull_request_target"):
        # connector-reviewer reviews only when asked. A push never spends money on its own.
        return Decision(
            False,
            reason="connector-reviewer runs only when invoked: comment `/review` on the PR",
        )

    if event_name == "issue_comment":
        issue = event.get("issue") or EMPTY
        comment = event.get("comment") or EMPTY
        if event.get("action") != "created" or "pull_request" not in issue:
            return Decision(False, reason="not a new PR comment")
        words = str(comment.get("body") or "").strip().split()
        if not words or words[0].lower() != COMMAND:
            return Decision(False, reason="comment is not addressed to /review")
        number = int(issue["number"])
        comment_id = int(comment.get("id") or 0)
        actor = str((comment.get("user") or EMPTY).get("login") or "")
        pr_author = str((issue.get("user") or EMPTY).get("login") or "")
        allowed, why_not, reply = _may_ask(comment, trusted_apps, can_push)
        if not allowed:
            return Decision(
                False,
                number,
                reason=why_not,
                comment_id=comment_id,
                actor=actor,
                reply=reply,
            )
        if len(words) > 1 and words[1].lower() == "dismiss":
            ids = [w for w in words[2:] if _FINDING_ID.match(w)]
            reason = plain(" ".join(w for w in words[2:] if not _FINDING_ID.match(w)))
            return Decision(
                bool(ids and reason),
                number,
                reason="" if ids and reason else DISMISS_USAGE,
                comment_id=comment_id,
                dismiss=ids,
                dismiss_reason=reason[:REASON_KEEP],
                actor=actor,
                pr_author=pr_author,
                reply=not (ids and reason),
            )
        if len(words) > 1 and words[1].lower() == "recheck":
            rest: list[str] = [str(w) for w in words[2:]]
            verify_budget = bool(rest) and rest[0].lower() == VERIFY_BUDGET
            if verify_budget:
                rest = rest[1:]
            ids = [w for w in rest if _FINDING_ID.match(w)]
            reason = " ".join(w for w in rest if not _FINDING_ID.match(w)).strip()
            return Decision(
                bool(ids),
                number,
                reason="" if ids else RECHECK_USAGE,
                comment_id=comment_id,
                recheck=ids,
                recheck_reason=reason[:REASON_KEEP],
                verify_budget=verify_budget,
                actor=actor,
                pr_author=pr_author,
                reply=not ids,
            )
        if len(words) > 1 and words[1].lower() == "waive":
            reason = plain(" ".join(words[2:]))
            return Decision(
                bool(reason),
                number,
                reason="" if reason else WAIVE_USAGE,
                comment_id=comment_id,
                waive=True,
                waive_reason=reason[:REASON_KEEP],
                actor=actor,
                pr_author=pr_author,
                reply=not reason,
            )
        options = [w.lower() for w in words[1:3]]
        if options and options[0] not in REVIEW_OPTIONS:
            # Not a word this command knows: tell the commenter, review nothing.
            return Decision(
                False,
                number,
                reason=REVIEW_USAGE,
                comment_id=comment_id,
                actor=actor,
                pr_author=pr_author,
                reply=True,
            )
        force = bool(options) and options[0] == "force"
        return Decision(
            True,
            number,
            force=force,
            comment_id=comment_id,
            verify_budget=VERIFY_BUDGET in options,
            actor=actor,
            pr_author=pr_author,
        )

    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or EMPTY
        return Decision(
            True,
            int(inputs["pr"]),
            force=str(inputs.get("force", "false")).lower() == "true",
        )

    return Decision(False, reason=f"event {event_name!r} is not handled")
