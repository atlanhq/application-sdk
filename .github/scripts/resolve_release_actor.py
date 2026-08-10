#!/usr/bin/env python3
"""Resolve the human to attribute a Global Marketplace release to.

Why this exists
---------------
GM renders the ``created_by`` field of a publish body as the *Created by:* line
of the "Release pending approval" Slack message, and @-mentions that person so
the release actually reaches someone who can approve it. GM resolves the field
as: work email -> Slack email lookup, else GitHub login -> its static
``GH_USERNAME_TO_SLACK_ID`` map, else plain text with no mention.

Every automated release in the fleet is cut by the ``atlan-app-fleet`` GitHub
App: ``release-version-bump.yaml`` opens the bump PR as the App, and
``tag-and-release.yaml`` creates the GitHub Release with the App's token. So on
the ``release: published`` build that actually publishes, ``triggering_actor``
is ``atlan-app-fleet[bot]`` — a login GM cannot map to a Slack user, which is
why those messages mention nobody.

The human in that chain is whoever *merged* the release PR. This script walks
the build SHA back to its pull request and prefers that merger.

Resolution chain
----------------
For ``workflow_dispatch``/``schedule`` the trigger is a deliberate human act, so
the triggering actor is the most accurate attribution and is tried first. For
every other event (``push``, ``release``) the trigger is automation, so the PR
merger leads:

    deliberate events:  triggering_actor -> merged_by -> PR author
    automated events:   merged_by -> PR author -> triggering_actor

Bots are skipped at every position; if nothing human is found the output is
empty and the caller omits ``created_by`` entirely (GM already treats absent as
"no attribution" and falls back to plain text).

Note that ``GET /repos/{repo}/commits/{sha}/pulls`` does *not* carry
``merged_by`` — only the single-PR endpoint does — hence the second call.

This script never fails the build: a release must not be blocked because an
attribution lookup 404'd. All errors degrade to a less specific actor, or to
no actor at all.

Environment:
    GITHUB_REPOSITORY   ``owner/repo`` of the app being released
    GITHUB_SHA          commit the build is for
    GITHUB_EVENT_NAME   event that triggered the run
    TRIGGERING_ACTOR    ``github.triggering_actor``
    GH_TOKEN            consumed by ``gh`` for auth (not read here directly)
    GITHUB_OUTPUT       written with ``created_by=<login-or-email>``
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess]

#: Events where the run was started by a person doing something on purpose, so
#: the triggering actor outranks anything inferred from git history.
DELIBERATE_EVENTS = frozenset({"workflow_dispatch", "schedule", "repository_dispatch"})


def is_bot(user: dict[str, Any] | None) -> bool:
    """True when a GitHub user object is a bot rather than a person.

    Checks both signals because they disagree in practice: GitHub App identities
    report ``type: "Bot"`` and carry a ``[bot]`` login suffix, but a login is all
    we have when the actor came from ``github.triggering_actor`` and was never
    looked up.
    """
    if not user:
        return True
    if str(user.get("type", "")).lower() == "bot":
        return True
    return str(user.get("login", "")).endswith("[bot]")


def gh_api(path: str, runner: Runner = subprocess.run) -> Any | None:
    """GET a GitHub API path, returning parsed JSON or None on any failure.

    ``gh api`` writes its JSON *error* body to stdout on a non-2xx, so the
    return code — not the presence of output — decides success. Without that
    gate an error payload would parse cleanly and be mistaken for a result.
    """
    try:
        result = runner(
            ["gh", "api", path],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as e:  # gh missing from the runner image
        print(f"::warning::gh api {path} could not run: {e}")
        return None
    if result.returncode != 0:
        stderr = (result.stderr or "").strip().splitlines()
        detail = stderr[-1] if stderr else f"exit {result.returncode}"
        print(f"::warning::gh api {path} failed: {detail}")
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        print(f"::warning::gh api {path} returned non-JSON output")
        return None


def find_pull_request(
    repo: str, sha: str, runner: Runner = subprocess.run
) -> dict[str, Any] | None:
    """Return the full PR object for the PR that put ``sha`` on its branch.

    Two calls: the list endpoint maps commit -> PR but omits ``merged_by``, so
    the winning number is re-fetched through the single-PR endpoint.

    The list can hold more than one PR when a commit is reachable from several
    (e.g. a branch merged into another branch that was then merged to main), so
    prefer the PR whose own merge commit *is* this SHA before falling back to
    the first entry.
    """
    if not repo or not sha:
        return None
    candidates = gh_api(f"repos/{repo}/commits/{sha}/pulls", runner)
    if not isinstance(candidates, list) or not candidates:
        return None
    exact = [
        c
        for c in candidates
        if isinstance(c, dict) and c.get("merge_commit_sha") == sha
    ]
    chosen = (exact or candidates)[0]
    number = chosen.get("number") if isinstance(chosen, dict) else None
    if not number:
        return None
    full = gh_api(f"repos/{repo}/pulls/{number}", runner)
    # Fall back to the list entry when the detail fetch fails — it still carries
    # the PR author, which is one rung down the chain but better than nothing.
    return full if isinstance(full, dict) else chosen


def public_email(login: str, runner: Runner = subprocess.run) -> str:
    """Return the user's public GitHub email, or "" when they publish none.

    Worth the extra call even though most Atlan members keep their email
    private: GM's email path resolves *any* Slack user, while the login path
    only resolves logins present in its hand-maintained map. An email, when we
    can get one, is strictly the better identifier.
    """
    if not login:
        return ""
    user = gh_api(f"users/{login}", runner)
    if not isinstance(user, dict):
        return ""
    return str(user.get("email") or "").strip()


def resolve_actor(
    repo: str,
    sha: str,
    event_name: str,
    triggering_actor: str,
    runner: Runner = subprocess.run,
) -> str:
    """Return the login to attribute the release to, or "" if none is human."""
    actor_user = {"login": triggering_actor} if triggering_actor else None

    pr = find_pull_request(repo, sha, runner)
    merger = pr.get("merged_by") if isinstance(pr, dict) else None
    author = pr.get("user") if isinstance(pr, dict) else None

    if event_name in DELIBERATE_EVENTS:
        chain = [actor_user, merger, author]
    else:
        chain = [merger, author, actor_user]

    for candidate in chain:
        if isinstance(candidate, dict) and not is_bot(candidate):
            login = str(candidate.get("login") or "").strip()
            if login:
                return login
    return ""


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    sha = os.environ.get("GITHUB_SHA", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    triggering_actor = os.environ.get("TRIGGERING_ACTOR", "").strip()

    try:
        login = resolve_actor(repo, sha, event_name, triggering_actor, runner)
        created_by = public_email(login, runner) or login if login else ""
    except Exception as e:  # never block a release on attribution
        print(f"::warning::release actor resolution failed: {e}")
        created_by = ""

    if created_by:
        print(f"Attributing release to {created_by}")
    else:
        print(
            "::warning::No human could be attributed to this release — the Slack "
            "approval message will show no @-mention."
        )

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"created_by={created_by}\n")
    else:
        print(created_by)
    return 0


if __name__ == "__main__":
    sys.exit(main())
