"""Decide from a GitHub event whether lens runs, on which PR, and whether forced.

Kept out of the workflow YAML (docs/standards/ci.md: no branching shell in
`run:` blocks) and tested here instead.

lens reviews only when asked — a push never spends money on its own:

- `issue_comment` on a PR whose body starts with `@lens` from an OWNER,
  MEMBER or COLLABORATOR: run. Each run continues from the last one (it
  reviews only commits since the last reviewed head). `@lens force` also
  bypasses the unchanged-head and round-cap admission rules and redoes the
  approach check (the $ cap still holds — force cannot buy more budget).
- `workflow_dispatch` with a PR number: run (maintainers, from the Actions tab).
- Anything else, including every `pull_request*` event: do not run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass
class Decision:
    run: bool
    pr: int = 0
    force: bool = False
    reason: str = ""


def decide(event_name: str, event: dict[str, Any], repo: str) -> Decision:
    if event_name in ("pull_request", "pull_request_target"):
        # lens reviews only when asked. A push never spends money on its own.
        return Decision(
            False, reason="lens runs only when invoked: comment `@lens` on the PR"
        )

    if event_name == "issue_comment":
        issue = event.get("issue") or {}
        comment = event.get("comment") or {}
        if event.get("action") != "created" or "pull_request" not in issue:
            return Decision(False, reason="not a new PR comment")
        words = (comment.get("body") or "").strip().split()
        if not words or words[0].lower() != "@lens":
            return Decision(False, reason="comment is not addressed to @lens")
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
        force = len(words) > 1 and words[1].lower() == "force"
        return Decision(True, int(issue["number"]), force=force)

    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        return Decision(
            True,
            int(inputs["pr"]),
            force=str(inputs.get("force", "false")).lower() == "true",
        )

    return Decision(False, reason=f"event {event_name!r} is not handled")
