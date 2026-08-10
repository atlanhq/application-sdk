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
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

Runner = Callable[..., subprocess.CompletedProcess]

#: Events where the run was started by a person doing something on purpose, so
#: the triggering actor outranks anything inferred from git history.
DELIBERATE_EVENTS = frozenset({"workflow_dispatch", "schedule", "repository_dispatch"})

#: Machine accounts that commit as ordinary users — no ``[bot]`` suffix and
#: ``type: "User"`` — so ``is_bot`` cannot see them. Only the *contributor* list
#: filters on this: `atlan-ci` authors the version-bump commit in every release
#: range, and crediting it as a contributor is pure noise.
#:
#: Deliberately NOT applied to the merger chain. GM maps `atlan-ci` to a real
#: Slack ID, and if a release genuinely was cut by it, naming it beats naming
#: nobody. Keeping the two filters separate also means this list cannot change
#: who `created_by` resolves to.
AUTOMATION_LOGINS = frozenset({"atlan-ci", "atlan-bot", "web-flow"})

#: Slack renders a long mention list as an unreadable wall, and Block Kit caps
#: `section.fields` at 10 entries. Truncation is reported, never silent.
MAX_CONTRIBUTORS = 8

#: `compare` returns at most 250 commits. A range that large means the tag
#: walk found the wrong base, so treat it as unusable rather than crediting
#: a year of history to one release.
COMPARE_COMMIT_LIMIT = 250


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


_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def parse_semver(tag: str) -> tuple[int, int, int, int, str] | None:
    """Parse ``v1.2.3`` / ``v1.2.3-rc1`` into a sortable key, or None.

    The prerelease slot sorts *below* the same version's final release (0 vs 1),
    matching semver — so ``v1.2.3-rc1`` is a valid predecessor of ``v1.2.3``.
    """
    m = _SEMVER.match((tag or "").strip())
    if not m:
        return None
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1, pre or "")


def previous_tag(
    repo: str, current_tag: str, runner: Runner = subprocess.run
) -> str | None:
    """Return the semver tag immediately preceding ``current_tag``.

    Sorts by parsed semver rather than trusting list order: the tags endpoint
    is not semver-ordered (``v0.10.0`` would sort before ``v0.9.0``
    lexically), and the releases endpoint reorders on re-cut because
    ``tag-and-release.yaml`` deletes and recreates a release of the same
    version.

    Bounded to the first 100 tags — enough to find the immediate predecessor
    of the newest tag. Returns None if the current tag isn't in that window,
    so the caller degrades instead of comparing against a wrong base.
    """
    current = parse_semver(current_tag)
    if not repo or not current:
        return None
    tags = gh_api(f"repos/{repo}/tags?per_page=100", runner)
    if not isinstance(tags, list):
        return None

    parsed = []
    for t in tags:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "")
        key = parse_semver(name)
        if key:
            parsed.append((key, name))

    earlier = sorted({p for p in parsed if p[0] < current})
    if not earlier:
        print(
            f"::warning::No tag older than {current_tag} found — skipping contributors."
        )
        return None
    return earlier[-1][1]


def resolve_contributors(
    repo: str,
    base_tag: str,
    head_sha: str,
    exclude: set[str],
    runner: Runner = subprocess.run,
) -> list[str]:
    """Human logins who authored commits in ``base_tag..head_sha``.

    One ``compare`` call, the same endpoint ``update_changelog.py`` walks to
    build the release's CHANGELOG section — so the people credited in Slack and
    the people credited in the release notes are derived from the same range by
    construction.

    ``exclude`` drops the merger, who is already named as ``created_by`` and
    almost always also authored commits in the range.
    """
    if not repo or not base_tag or not head_sha:
        return []
    data = gh_api(f"repos/{repo}/compare/{base_tag}...{head_sha}", runner)
    if not isinstance(data, dict):
        return []

    total = data.get("total_commits")
    if isinstance(total, int) and total > COMPARE_COMMIT_LIMIT:
        print(
            f"::warning::{base_tag}...{head_sha} spans {total} commits — "
            "base tag looks wrong, skipping contributors."
        )
        return []

    ordered: list[str] = []
    for commit in data.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        author = commit.get("author")
        if not isinstance(author, dict) or is_bot(author):
            continue
        login = str(author.get("login") or "").strip()
        if not login or login in AUTOMATION_LOGINS or login in exclude:
            continue
        if login not in ordered:
            ordered.append(login)

    if len(ordered) > MAX_CONTRIBUTORS:
        print(
            f"::warning::{len(ordered)} contributors in {base_tag}...{head_sha} — "
            f"crediting the first {MAX_CONTRIBUTORS}."
        )
        ordered = ordered[:MAX_CONTRIBUTORS]
    return ordered


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


def contributors_for_event(
    repo: str,
    sha: str,
    release_tag: str,
    merger_login: str,
    runner: Runner = subprocess.run,
) -> list[str]:
    """Humans whose work ships in this release, excluding the merger.

    Semver releases carry a tag, so the range is the previous tag to this
    commit — every PR merged since the last release. A CD (untagged) publish
    ships exactly one merge, so the range collapses to that PR's author.
    """
    exclude = {merger_login} if merger_login else set()

    if release_tag:
        base = previous_tag(repo, release_tag, runner)
        if base:
            return resolve_contributors(repo, base, sha, exclude, runner)
        return []

    pr = find_pull_request(repo, sha, runner)
    author = pr.get("user") if isinstance(pr, dict) else None
    if isinstance(author, dict) and not is_bot(author):
        login = str(author.get("login") or "").strip()
        if login and login not in exclude and login not in AUTOMATION_LOGINS:
            return [login]
    return []


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    sha = os.environ.get("GITHUB_SHA", "").strip()
    event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip()
    triggering_actor = os.environ.get("TRIGGERING_ACTOR", "").strip()
    release_tag = os.environ.get("RELEASE_TAG", "").strip()

    login = ""
    try:
        login = resolve_actor(repo, sha, event_name, triggering_actor, runner)
        created_by = public_email(login, runner) or login if login else ""
    except Exception as e:  # never block a release on attribution
        print(f"::warning::release actor resolution failed: {e}")
        created_by = ""

    try:
        contributors = contributors_for_event(repo, sha, release_tag, login, runner)
    except Exception as e:  # contributors are a nicety; the merger is the point
        print(f"::warning::contributor resolution failed: {e}")
        contributors = []

    if created_by:
        print(f"Attributing release to {created_by}")
    else:
        print(
            "::warning::No human could be attributed to this release — the Slack "
            "approval message will show no @-mention."
        )
    if contributors:
        print(f"Crediting contributors: {', '.join(contributors)}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"created_by={created_by}\n")
            fh.write(f"authored_by={','.join(contributors)}\n")
    else:
        print(created_by)
        print(",".join(contributors))
    return 0


if __name__ == "__main__":
    sys.exit(main())
