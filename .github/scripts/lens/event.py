"""Decide from a GitHub event whether lens runs, on which PR, and whether forced.

Kept out of the workflow YAML (docs/standards/ci.md: no branching shell in
`run:` blocks) and tested here instead.

lens reviews only when asked — a push never spends money on its own:

- `issue_comment` on a PR whose body starts with `/lens` from an OWNER,
  MEMBER or COLLABORATOR: run. Each run continues from the last one (it
  reviews only commits since the last reviewed head). `/lens force` also
  bypasses the unchanged-head and round-cap admission rules and redoes the
  approach check (the $ cap still holds — force cannot buy more budget).
- `/lens dismiss F-1a2b3c [F-…] <reason>`: close findings the team decided not to
  fix, with the reason on record. No model call. A blocking (critical/high) finding
  cannot be dismissed by the PR's own author: someone else has to agree.
- `workflow_dispatch` with a PR number: run (maintainers, from the Actions tab).
- Anything else, including every `pull_request*` event: do not run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class Decision:
    run: bool
    pr: int = 0
    force: bool = False
    reason: str = ""
    comment_id: int = 0  # the `/lens` comment to react on (0 = none, e.g. a dispatch)
    dismiss: list[str] = field(
        default_factory=list
    )  # finding ids to close, for `/lens dismiss`
    dismiss_reason: str = ""
    actor: str = ""  # who asked
    pr_author: str = ""


_FINDING_ID = re.compile(r"^F-[0-9a-f]{6}$")
DISMISS_USAGE = "usage: `/lens dismiss F-1a2b3c [F-…] <reason>` — at least one finding id and a reason"


def decide(event_name: str, event: dict[str, Any], repo: str) -> Decision:
    if event_name in ("pull_request", "pull_request_target"):
        # lens reviews only when asked. A push never spends money on its own.
        return Decision(
            False, reason="lens runs only when invoked: comment `/lens` on the PR"
        )

    if event_name == "issue_comment":
        issue = event.get("issue") or {}
        comment = event.get("comment") or {}
        if event.get("action") != "created" or "pull_request" not in issue:
            return Decision(False, reason="not a new PR comment")
        words = (comment.get("body") or "").strip().split()
        if not words or words[0].lower() != "/lens":
            return Decision(False, reason="comment is not addressed to /lens")
        if comment.get("author_association") not in TRUSTED:
            return Decision(
                False,
                int(issue["number"]),
                reason="only owners, members and collaborators can trigger lens",
            )
        if (comment.get("user") or {}).get("type") == "Bot":
            return Decision(
                False, int(issue["number"]), reason="bots cannot trigger lens"
            )
        if len(words) > 1 and words[1].lower() == "dismiss":
            ids = [w for w in words[2:] if _FINDING_ID.match(w)]
            reason = " ".join(w for w in words[2:] if not _FINDING_ID.match(w)).strip()
            return Decision(
                bool(ids and reason),
                int(issue["number"]),
                reason="" if ids and reason else DISMISS_USAGE,
                comment_id=int(comment.get("id") or 0),
                dismiss=ids,
                dismiss_reason=reason[:300],
                actor=str((comment.get("user") or {}).get("login") or ""),
                pr_author=str((issue.get("user") or {}).get("login") or ""),
            )
        force = len(words) > 1 and words[1].lower() == "force"
        return Decision(
            True,
            int(issue["number"]),
            force=force,
            comment_id=int(comment.get("id") or 0),
        )

    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        return Decision(
            True,
            int(inputs["pr"]),
            force=str(inputs.get("force", "false")).lower() == "true",
        )

    return Decision(False, reason=f"event {event_name!r} is not handled")
