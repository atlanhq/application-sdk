#!/usr/bin/env python3
"""Resolve who a marketplace publish is attributed to ("Created by:").

The Global Marketplace shows ``created_by`` in the Slack approval message for a
release, so it needs to name the *human who owns the change* — not whatever
identity happened to move the bits.

For ``workflow_dispatch`` and ``schedule`` that is simply the triggering actor:
the run was started deliberately by someone, and that someone is the answer.

``push`` (merge-to-main auto-publish) and ``release`` (the tag flow) are the
hard cases, and they share a fix. Version-bump
PRs are opened by the Atlan Fleet App bot, so the PR *author* is a bot and the
person who owns the release is the one who **merged** it. That name is only
reachable in two hops:

1. ``GET /repos/{repo}/commits/{sha}/pulls`` — the PR(s) the merge SHA belongs
   to. This list response carries ``user`` but **not** ``merged_by``: the field
   is present and ``null`` even for a merged PR, so reading it here silently
   yields nobody and the fallback fires every time.
2. ``GET /repos/{repo}/pulls/{number}`` — the single-PR response, which is the
   only one that populates ``merged_by``.

Auto-merge and merge queues are why the second hop is worth making rather than
just trusting ``github.triggering_actor``: when the push is performed by
``github-merge-queue[bot]`` (or by auto-merge on someone else's behalf) the
triggering actor is the bot, while ``merged_by`` still names the human. When
there is no associated PR at all (a direct push to main), the triggering actor
*is* the person who pushed, and that is the fallback.

Finally, GM prefers an email address over a login, so the resolved actor's
public email is looked up best-effort. Most accounts do not publish one; the
login is the fallback, and every lookup failure degrades in that direction
rather than failing the publish.

Usage (from within a workflow step)::

    python3 .github/scripts/resolve_release_actor.py \
      --event-name push --repo owner/name --sha "$GITHUB_SHA" \
      --triggering-actor someone >> "$GITHUB_OUTPUT"

Writes ``created_by=<email-or-login>`` to stdout; diagnostics go to stderr.
Requires ``GH_TOKEN`` in the environment for the ``gh`` calls.

See ``docs/standards/ci.md`` for why this lives in a tested script rather than
inline workflow shell — an unreachable field in inline `gh --jq` is exactly the
kind of silent no-op a regression test catches and a green workflow does not.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Callable

PUSH_EVENT = "push"
RELEASE_EVENT = "release"

# Events whose triggering actor is not the person who owns the release, so the
# human has to be recovered from the PR behind the SHA.
#
# ``push`` is the merge-to-main auto-publish flow. ``release`` is the tag flow:
# the tag is cut by ``tag-and-release`` when a release-labelled PR merges, so
# the GitHub Release is authored by the Fleet App bot and ``triggering_actor``
# on the resulting ``release: published`` run is that bot — never a human. In
# both cases GITHUB_SHA is the merge commit of the PR that caused the release,
# so the same two-hop lookup recovers the person who merged it.
_PR_DERIVED_EVENTS = (PUSH_EVENT, RELEASE_EVENT)


def _run_gh(args: list) -> str:
    """Run ``gh`` and return stdout, or "" on any failure.

    The single seam the tests stub. ``gh`` writes its JSON error body to stdout
    on a non-2xx, so the return code — not the presence of output — decides
    whether there is anything worth parsing.
    """
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError as exc:
        # ``gh`` missing from the PATH (or otherwise unexecutable) must degrade
        # like every other lookup failure, not crash the step under pipefail.
        print(f"::warning::gh could not be run: {exc}", file=sys.stderr)
        return ""
    if result.returncode != 0:
        if result.stderr:
            print(
                f"::warning::gh {' '.join(args[:2])} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
        return ""
    return result.stdout


def _api_json(path: str, run: Callable[[list], str]) -> object:
    """GET one API path and parse it, or return ``None`` on any failure."""
    raw = run(["api", path])
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"::warning::unparseable response from {path}", file=sys.stderr)
        return None


def pr_number_for_sha(repo: str, sha: str, run: Callable[[list], str]) -> int | None:
    """The number of the first PR associated with ``sha``, if any."""
    payload = _api_json(f"repos/{repo}/commits/{sha}/pulls", run)
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    number = first.get("number") if isinstance(first, dict) else None
    return number if isinstance(number, int) else None


def merger_of_pr(repo: str, number: int, run: Callable[[list], str]) -> str | None:
    """The login that merged PR ``number``, if it is merged.

    Only the single-PR endpoint populates ``merged_by``; an open (or
    closed-unmerged) PR returns ``null`` here legitimately.
    """
    payload = _api_json(f"repos/{repo}/pulls/{number}", run)
    if not isinstance(payload, dict):
        return None
    merged_by = payload.get("merged_by")
    if not isinstance(merged_by, dict):
        return None
    login = merged_by.get("login")
    return login if isinstance(login, str) and login else None


def resolve_actor(
    event_name: str,
    repo: str,
    sha: str,
    triggering_actor: str,
    run: Callable[[list], str] | None = None,
) -> str:
    """The login to attribute the release to."""
    run = run or _run_gh
    if event_name not in _PR_DERIVED_EVENTS:
        return triggering_actor
    number = pr_number_for_sha(repo, sha, run)
    if number is None:
        print(
            f"::notice::no PR associated with {sha[:12]} — "
            f"attributing to the triggering actor",
            file=sys.stderr,
        )
        return triggering_actor
    merger = merger_of_pr(repo, number, run)
    if merger is None:
        print(
            f"::warning::PR #{number} for {sha[:12]} reports no merged_by — "
            f"attributing to the triggering actor",
            file=sys.stderr,
        )
        return triggering_actor
    return merger


def public_email(actor: str, run: Callable[[list], str] | None = None) -> str | None:
    """The actor's public email, when they publish one."""
    run = run or _run_gh
    if not actor:
        return None
    payload = _api_json(f"users/{actor}", run)
    if not isinstance(payload, dict):
        return None
    email = payload.get("email")
    return email if isinstance(email, str) and email else None


def resolve(
    event_name: str,
    repo: str,
    sha: str,
    triggering_actor: str,
    run: Callable[[list], str] | None = None,
) -> str:
    """The value GM stores as ``created_by`` — an email when one is public."""
    # Resolved per call, not bound as a default, so the ``_run_gh`` seam stays
    # stubbable from ``main`` down.
    actor = resolve_actor(event_name, repo, sha, triggering_actor, run)
    return public_email(actor, run) or actor


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve the actor a marketplace publish is attributed to."
    )
    parser.add_argument("--event-name", required=True, help="github.event_name")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--sha", default="", help="github.sha (push events)")
    parser.add_argument(
        "--triggering-actor",
        required=True,
        help="github.triggering_actor — the fallback attribution.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    created_by = resolve(args.event_name, args.repo, args.sha, args.triggering_actor)
    # stdout is a strict contract: `created_by=<value>` and nothing else, since
    # the caller redirects it into $GITHUB_OUTPUT and the runner rejects any
    # line there without an `=`.
    print(f"::notice::release attributed to {created_by}", file=sys.stderr)
    print(f"created_by={created_by}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
