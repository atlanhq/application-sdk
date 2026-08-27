#!/usr/bin/env python3
"""Carry Renovate's ``renovate/artifacts`` status forward onto our own commit.

Why this is needed
------------------
``renovate-auto-approve-reusable.yml`` condition (f) requires
``renovate/artifacts`` to be ``success`` *on the SHA it evaluates*, and reads an
absent context as not-green — deliberately, because a post-upgrade command that
was skipped and one that ran clean are otherwise indistinguishable
(``renovate_approval_conditions.classify_artifact_state``).

Commit statuses are per-SHA and Renovate only stamps commits it authored. So the
moment any in-repo workflow adds a commit to a Renovate branch, the head moves
off the stamped SHA and the gate withholds forever. That is not hypothetical: on
application-sdk#3216 atlan-ci posted three approvals, each dismissed by the next
push, and the PR finally merged on a human's approval after the since-removed
``dependabot-requirements-sync.yaml`` added the last commit. Every lock-refresh
PR that gets a bot commit has needed a human ever since.

What this does, and what it must never do
-----------------------------------------
It republishes a state it **read from our commit's parent**, unchanged. It is a
carry-forward, not an assertion: if Renovate said ``success`` about the tree we
started from, that fact is still true of the tree we produced — we changed the
lock's *contents*, not whether Renovate's own artifact update worked.

Everything else publishes nothing, which leaves the context missing and the gate
withholding — the same fail-closed direction as the rest of the bound (FND-367):

* parent is ``failure`` — Renovate really did hit an artifact error, and that
  must keep blocking. This is the state application-sdk's lane sits in whenever
  the preset declares a post-upgrade command Mend cannot run.
* parent is ``pending``, some other state, or carries no such context at all.
* the parent's status cannot be read at all.

It is also a no-op when our head already carries the context, which is the case
whenever the bound found nothing to change and made no commit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ARTIFACT_CONTEXT = "renovate/artifacts"
SUCCESS = "success"

#: Renovate pushes the branch first and publishes its statuses afterwards, so the
#: parent can legitimately be unstamped for a few seconds after the push that
#: triggered us. Absent is otherwise indistinguishable from not-yet, and treating
#: not-yet as absent would withhold approval on a PR that is entirely healthy —
#: until the next rebase, which for a lock-refresh branch may never come. So poll
#: briefly, then fail closed. Only ABSENCE is waited on: a context that is
#: present and not `success` is a verdict, not a race.
POLL_ATTEMPTS = 12
POLL_INTERVAL_SECONDS = 10

#: Test seam. Patching `time.sleep` globally would slow every other test in the
#: process that happens to sleep; a module attribute keeps the substitution local.
sleep = time.sleep


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


def git(*args: str) -> str | None:
    result = run(["git", *args])
    return result.stdout.strip() if result.returncode == 0 else None


def artifact_state(repo: str, sha: str) -> str | None:
    """The ``renovate/artifacts`` state on ``sha``, or None if absent/unreadable.

    Fails closed in both directions: an unreadable payload and an absent context
    are both None, and the only value the caller acts on is an explicit
    ``success``.
    """
    result = run(["gh", "api", f"repos/{repo}/commits/{sha}/status"])
    if result.returncode != 0:
        print(
            f"Could not read statuses for {sha}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Status payload for {sha} was not JSON.", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        return None
    for entry in payload.get("statuses") or []:
        if isinstance(entry, dict) and entry.get("context") == ARTIFACT_CONTEXT:
            state = entry.get("state")
            return state if isinstance(state, str) else None
    return None


def await_artifact_state(repo: str, sha: str, attempts: int, interval: float):
    """``artifact_state``, retried while the context is merely ABSENT.

    Returns as soon as any state is readable, so a genuine ``failure`` is acted on
    immediately rather than waited out. Returns None once the attempts are spent,
    which is the fail-closed answer.
    """
    for attempt in range(1, attempts + 1):
        state = artifact_state(repo, sha)
        if state is not None:
            return state
        if attempt < attempts:
            print(
                f"{ARTIFACT_CONTEXT} not yet published on {sha[:7]} "
                f"(attempt {attempt}/{attempts}); Renovate pushes the branch "
                "before it sets statuses, so waiting.",
            )
            sleep(interval)
    return None


def publish(repo: str, sha: str, parent: str, target_url: str) -> int:
    """Post the carried status. The description names its provenance on purpose:
    the whole point is that a reader can tell this was carried, and from where."""
    command = [
        "gh",
        "api",
        "-X",
        "POST",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"context={ARTIFACT_CONTEXT}",
        "-f",
        f"state={SUCCESS}",
        "-f",
        f"description=Carried from {parent[:7]} after the release-age bound",
    ]
    if target_url:
        command += ["-f", f"target_url={target_url}"]
    result = run(command)
    if result.returncode != 0:
        print(f"Could not publish the status: {result.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"Carried {ARTIFACT_CONTEXT}={SUCCESS} from {parent[:7]} onto {sha[:7]}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument(
        "--target-url",
        default="",
        help="Link recorded on the published status, so a reader can reach the "
        "run that carried it.",
    )
    parser.add_argument("--poll-attempts", type=int, default=POLL_ATTEMPTS)
    parser.add_argument("--poll-interval", type=float, default=POLL_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    if not args.repo:
        print("No repository given and GITHUB_REPOSITORY is unset.", file=sys.stderr)
        return 1

    head = git("rev-parse", "HEAD")
    if head is None:
        print("Cannot resolve HEAD.", file=sys.stderr)
        return 1

    if artifact_state(args.repo, head) is not None:
        # The bound made no commit, so HEAD is still Renovate's own stamped
        # commit. Nothing to carry, and re-posting would only overwrite a state
        # Renovate owns.
        print(f"{ARTIFACT_CONTEXT} is already present on HEAD — nothing to carry.")
        return 0

    parent = git("rev-parse", "HEAD^")
    if parent is None:
        print("HEAD has no parent, so there is no status to carry.", file=sys.stderr)
        return 0

    state = await_artifact_state(
        args.repo, parent, args.poll_attempts, args.poll_interval
    )
    if state != SUCCESS:
        # Withholding is the whole design: the gate reads the missing context as
        # not-green and declines to approve, exactly as it would have without us.
        print(
            f"{ARTIFACT_CONTEXT} on the parent {parent[:7]} is "
            f"{state or 'absent'!r}, not {SUCCESS!r} — publishing nothing, so the "
            "approval gate keeps withholding."
        )
        return 0

    return publish(args.repo, head, parent, args.target_url)


if __name__ == "__main__":
    sys.exit(main())
