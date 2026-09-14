#!/usr/bin/env python3
"""Delete lock-maintenance branches the next pass rebuilds better.

Two independent reasons: a lock refusal that has since expired (FND-909), and a
branch some other Renovate engine wrote over (FND-1985).

Runs once per repo, immediately *before* Renovate in the same matrix job of
``renovate.yaml``, so a reaped branch is rebuilt in the same pass — there is no
window where the repo has no lock PR, and no delete/recreate churn visible in
the PR timeline.

The problem
-----------
``renovate_uv_lock_bounded.withhold()`` refuses by writing a lock the image
build cannot install, which reds a required check and holds the branch. That is
correct and deliberate. What is missing is any way out: the tripwire carries no
clock, and Renovate re-runs ``postUpgradeTasks`` only when a package file
changes, the branch conflicts, or a human ticks rebase. So a refusal written at
T is still red at T+7d even though the condition that caused it expired at T+3d.

The fleet cron is every four hours against a three-day bound, so after any
successful lock merge the bound usually admits nothing on the next pass while
Renovate's unbounded resolve has moved — which is exactly the refusal condition.
A permanent freeze is therefore the *modal* outcome of the lane, not an edge
case. Measured 2026-08-28: five frozen PRs, including all four canonical apps.

Why not reap on a clock
-----------------------
``conformance.renovate.classify.bounded_lock_refusal_expired`` already detects
this shape, but it cannot tell which of the five refusal paths wrote the
tripwire, so it has to prove expiry by branch age (head older than the window).
That is sound, and too slow: it waits a full window before recovering, when the
bound may have admitted something four hours in. Recovering a day and a half
late on average defeats the reason the lane runs every four hours.

So the driver now stamps *why* it refused, and this reaps only the reason that
heals on its own. A yanked-pin wedge or a broken interpreter keeps its tripwire
and stays red for a human — reaping those would recycle them every four hours
and hide a standing fault behind a lane that looks busy.

The other shape: a foreign engine wrote the branch
--------------------------------------------------
``allowedCommands`` is an admin-only option, so it exists only for a runner we
own. Any OTHER Renovate engine reading the same ``renovate.json`` — the
Mend-hosted app, still installed across the org long after the fleet moved off
it — resolves the same preset, finds the same ``postUpgradeTasks``, and has
every one of them rejected. It then pushes to the SAME branch name, because
branch names come from the preset rather than from the engine. So a lock this
runner bounded correctly at 08:11 is replaced at 11:18 by one refreshed
unbounded, carrying a red ``renovate/artifacts``.

The controls hold: the auto-approve gate withholds its code-owner approval
while that status is red, so nothing merges.

Nothing recovers either, and the reason is worth recording exactly, because it
is not obvious and it is not a timing problem. Renovate applies two independent
checks to such a branch, and each alone is enough to wedge it. From the
atlan-netsuite-app job of 2026-09-14T14:15:44Z, on renovate/conformance-package::

    DEBUG: branch.isModified() = true
      "unrecognizedAuthors": ["29139614+renovate[bot]@users.noreply.github.com"]
    DEBUG: Branch has been edited but found no PR - skipping

First, the branch reads as HUMAN-EDITED: the head commit's author is not this
runner's, so Renovate treats it as a branch someone hand-modified and refuses to
touch it. Second, Renovate cannot see the PR at all — its PR list is scoped to
its own account, so in that same run it found #64 on the lock lane (author
``app/atlan-app-fleet``) and did not find #92 on this one (author
``app/renovate``). A branch it will not write, attached to a PR it cannot see.

Note what this is NOT: waiting on the base branch. ``rebaseWhen:
behind-base-branch`` cannot rescue it — atlan-athena-app's ``main`` had a
``latestCommitDate`` of 2026-09-02 while the branch sat wedged, so it was never
behind base to begin with. Renovate's own orphan pruning declines too, for the
same isModified reason: ``Orphan Branch is modified - skipping branch
deletion``. There is no clock and no trigger. Deleting the branch is the only
thing that clears it, which is why this script exists.

Scope: EVERY managed lane, not just the lock branch. The deadlock above is a
property of who wrote the branch, not of which lane it is, and it was first
diagnosed on renovate/conformance-package. The one exclusion is
``UNMANAGED_LANES`` — see there.

A backstop, not the fix. The fix is for no second engine to be installed on
these repos at all; this only bounds the damage while one is.

Safety
------
A *foreign engine* reap requires all of:

* the branch name starts with ``BRANCH_PREFIX`` and is not in
  ``UNMANAGED_LANES``,
* it has an open PR,
* and EVERY commit the branch carries over its merge base with main was written
  by an engine in ``FOREIGN_ENGINES``.

That last one is the load-bearing guard, and it is deliberately stricter than
checking the head alone: a human who pushes a fix on top of a foreign branch
keeps their work, because their commit is in the history even if it is not the
head. ``FOREIGN_ENGINES`` is an allowlist rather than "anyone who is not us", so
an author this script does not recognise means leave it alone. It reads the
author GitHub *resolved* for each commit, never the git author name inside the
commit object, which any pusher can set to anything.

A *self-healing refusal* reap is narrower and unchanged: the branch must be
exactly ``BRANCH``, the PR's only changed file a ``uv.lock``, whose
``[options]`` table carries a refusal stamp, whose stamped reason is in
``SELF_HEALING_REFUSALS``. A stamp exists on no other lane, so there is nothing
to generalise.

An *unstamped* tripwire is left alone. Locks refused before this change carry no
reason, and treating "no reason given" as self-healing is the one mistake that
would recycle a real wedge forever. Those are triaged by hand once; every
refusal written from now on is stamped.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from renovate_uv_lock_bounded import SELF_HEALING_REFUSALS  # noqa: E402

API_ROOT = "https://api.github.com"

# The shared preset's lockFileMaintenance branch. Hard-coded rather than taken
# as an argument: this script deletes branches, and the set it may delete from
# should not be widenable by a caller's typo. Only the REFUSAL reap is scoped to
# it — a refusal stamp exists nowhere else — while the foreign-engine reap
# covers every lane, because the deadlock it clears is not lane-specific.
BRANCH = "renovate/lock-file-maintenance"

# The preset's branchPrefix. A branch outside it is not Renovate's and is never
# a candidate, whoever wrote it.
BRANCH_PREFIX = "renovate/"

# Lanes the fleet runner does NOT manage, and therefore must never reap:
# self-hosted.js disables the github-actions manager (the fleet App deliberately
# holds no `workflows: write`), so a disabled manager never extracts and nothing
# would rebuild what we deleted. Reaping here would destroy, not recover — the
# one case where leaving a stranded branch in place is the correct outcome and a
# human decides whether to close it. Every other renovate/* lane IS managed, so
# deleting it is how the runner gets to rebuild it.
UNMANAGED_LANES = frozenset(
    {"renovate/github-actions", "renovate/major-github-actions"}
)

# The engine this runner authenticates as. A branch head some other engine wrote
# did not run our postUpgradeTasks — allowedCommands is admin-only, so no hosted
# engine can hold them — which means an unbounded lock and a red artifacts status.
FLEET_ENGINE = "atlan-app-fleet[bot]"

# Engines that are not ours but are known to write this branch. Deliberately an
# allowlist rather than "anything that is not FLEET_ENGINE": see Safety above.
FOREIGN_ENGINES = frozenset({"renovate[bot]"})

# The stamp withhold() writes, as it appears in the lock:
#     exclude-newer-span = "P3D"  # refusal: window-empty
STAMP = "# refusal:"

Fetch = Callable[[str, str, Optional[str]], object]


def _request(token: str, url: str, method: str = "GET") -> object:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else None


def options_lines(lock_text: str) -> list[str]:
    """The body lines of the lock's ``[options]`` table, or []."""
    body: list[str] = []
    in_options = False
    for line in lock_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[options]"):
            in_options = True
            continue
        # Any other table header ends [options]; [options.*] subtables do not.
        if (
            in_options
            and stripped.startswith("[")
            and not stripped.startswith("[options")
        ):
            break
        if in_options:
            body.append(stripped)
    return body


def is_tripwire(lock_text: str) -> bool:
    """Did ``withhold()`` write this ``[options]`` table, rather than uv?

    The same discriminator ``conformance.renovate.classify.lock_refusal_window``
    uses, and for the same reason: uv records both ``exclude-newer`` and
    ``exclude-newer-span`` when a repo declares a bound in its ``pyproject.toml``
    (``atlan-bw-app`` does), while the driver's tripwire is a lone
    ``exclude-newer-span``. Testing only for the presence of an ``[options]``
    table calls every natively-bounded repo a refusal.
    """
    keys = [line.partition("=")[0].strip() for line in options_lines(lock_text)]
    if "exclude-newer" in keys:
        return False
    return "exclude-newer-span" in keys


def refusal_reason(lock_text: str) -> Optional[str]:
    """The stamped refusal reason in a lock's tripwire, or None.

    None covers three genuinely different cases that all mean "do not reap":
    no tripwire at all (an ordinary lock, bounded or not), a table uv wrote
    itself, and a tripwire from before stamping existed. Collapsing them is
    deliberate — every one of them is a branch this script must not touch, and
    distinguishing them would invite a caller to act on the difference. The
    census, which does need to tell an unstamped tripwire from an ordinary
    lock, asks :func:`is_tripwire` for that instead.
    """
    if not is_tripwire(lock_text):
        return None
    for line in options_lines(lock_text):
        if STAMP in line:
            return line.split(STAMP, 1)[1].strip()
    return None


def lone_lock(files: list[str]) -> Optional[str]:
    """The path if ``files`` is exactly one ``uv.lock``, else None.

    Requiring a lone lock is what separates a refusal from an ordinary lock
    refresh that happens to carry an ``[options]`` table: withhold() writes the
    baseline back, so a refusal can never touch a second file.
    """
    if len(files) == 1 and files[0].rsplit("/", 1)[-1] == "uv.lock":
        return files[0]
    return None


def is_foreign_engine(login: Optional[str]) -> bool:
    """Did a Renovate engine that is not ours write this?

    ``None`` — GitHub could not resolve the commit to an account — is False, so
    an unattributable commit is kept rather than deleted.
    """
    return login in FOREIGN_ENGINES


def is_reapable_lane(branch: str) -> bool:
    """May a foreign-written branch on this lane be deleted?

    Two gates. Inside ``BRANCH_PREFIX``, so a branch that is not Renovate's is
    never a candidate. Outside ``UNMANAGED_LANES``, so we never delete something
    the runner cannot rebuild — there, deletion destroys rather than recovers.
    """
    return branch.startswith(BRANCH_PREFIX) and branch not in UNMANAGED_LANES


def foreign_only_history(commits: list[dict]) -> bool:
    """Was EVERY commit on this branch written by a foreign engine?

    Stricter than looking at the head, on purpose. If a human pushed a fix onto
    a foreign branch and a later foreign push landed on top, the head alone
    would say "foreign, reap it" and their commit would go with it. Requiring
    the whole history keeps that work.

    Empty is False: a branch with no commits over its base is not something to
    act on, and an API response that came back empty must not read as consent.
    """
    if not commits:
        return False
    return all(
        is_foreign_engine((commit.get("author") or {}).get("login"))
        for commit in commits
    )


def should_reap(files: list[str], lock_text: str) -> bool:
    """Is this PR a refusal that will clear itself on the next resolve?"""
    if lone_lock(files) is None:
        return False
    return refusal_reason(lock_text) in SELF_HEALING_REFUSALS


def find_foreign(
    token: str, repo: str, fetch: Fetch = _request
) -> list[tuple[dict, str]]:
    """Every open PR on ``repo`` whose branch a foreign engine wrote, with why.

    Lists ALL open PRs rather than querying one branch: the deadlock this clears
    is not lane-specific, and the fleet runner's own view is exactly the one
    that cannot see these — Renovate scopes its PR list to its own account, so
    a foreign PR is invisible to it. Reading the API directly is the point.
    """
    owner, name = repo.split("/", 1)
    prs = fetch(
        token,
        f"{API_ROOT}/repos/{owner}/{name}/pulls?state=open&per_page=100",
        None,
    )
    found: list[tuple[dict, str]] = []
    for pr in prs or []:  # type: ignore[union-attr]
        branch = pr["head"]["ref"]
        if not is_reapable_lane(branch):
            continue
        commits = fetch(
            token,
            f"{API_ROOT}/repos/{owner}/{name}/pulls/{pr['number']}/commits?per_page=100",
            None,
        )
        if not foreign_only_history(list(commits or [])):  # type: ignore[arg-type]
            continue
        author = (commits[-1].get("author") or {}).get("login")  # type: ignore[index]
        found.append(
            (pr, f"every commit on {branch} written by {author}, not {FLEET_ENGINE}")
        )
    return found


def find_refusal(
    token: str, repo: str, fetch: Fetch = _request
) -> Optional[tuple[dict, str]]:
    """The open lock-maintenance PR on ``repo`` if it is a self-healing refusal.

    Still scoped to ``BRANCH``: a refusal stamp is written by the bounded-lock
    driver and exists on no other lane, so there is nothing here to generalise.
    """
    owner, name = repo.split("/", 1)
    prs = fetch(
        token,
        f"{API_ROOT}/repos/{owner}/{name}/pulls?state=open&head={owner}:{BRANCH}",
        None,
    )
    if not prs:
        return None
    pr = prs[0]  # type: ignore[index]
    files_payload = fetch(
        token,
        f"{API_ROOT}/repos/{owner}/{name}/pulls/{pr['number']}/files?per_page=100",
        None,
    )
    files = [f["filename"] for f in files_payload]  # type: ignore[union-attr]
    path = lone_lock(files)
    if path is None:
        # Short-circuit before fetching contents. Same predicate should_reap
        # applies, called through the same helper so the two cannot drift.
        return None
    contents = fetch(
        token,
        f"{API_ROOT}/repos/{owner}/{name}/contents/{path}?ref={BRANCH}",
        None,
    )
    lock_text = base64.b64decode(contents["content"]).decode(  # type: ignore[index]
        errors="replace"
    )
    if not should_reap(files, lock_text):
        return None
    return pr, f"self-healing lock refusal ({refusal_reason(lock_text)})"


def find_reapable(
    token: str, repo: str, fetch: Fetch = _request
) -> list[tuple[dict, str]]:
    """Every branch on ``repo`` this pass should delete, each with its reason.

    The reason is the only record of WHICH shape fired, so it is produced here
    and logged verbatim rather than recomputed by the caller. Any transport
    failure raises: a reaper that silently does nothing on an API blip is
    indistinguishable from a healthy fleet, and this script's whole purpose is
    to be the thing that notices.
    """
    found = find_foreign(token, repo, fetch)
    already = {pr["head"]["ref"] for pr, _ in found}
    if BRANCH not in already:
        refusal = find_refusal(token, repo, fetch)
        if refusal is not None:
            found.append(refusal)
    return found


def is_dry_run(renovate_dry_run: str | None, flag: bool) -> bool:
    """Would this pass be a dry run?

    Renovate's own contract, mirrored rather than reinvented: the workflow sets
    ``RENOVATE_DRY_RUN`` to the literal string ``null`` for a live run and to a
    mode name (``full``, ``extract``, ``lookup``) otherwise. Anything that is
    not ``null`` is a dry run, so an unrecognised mode fails safe rather than
    deleting branches.

    This matters more here than for the Renovate step it precedes: a dry run
    that reaped for real would delete lock-maintenance branches across the whole
    matrix and then skip opening the replacements, which is strictly worse than
    the freeze this script exists to clear.
    """
    if flag:
        return True
    value = (renovate_dry_run or "").strip()
    return value not in ("", "null")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("TARGET_REPO", ""),
        help="owner/name; defaults to $TARGET_REPO",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "report what would be deleted and delete nothing. Also implied by "
            "$RENOVATE_DRY_RUN being anything other than 'null'."
        ),
    )
    args = parser.parse_args(argv)

    if not args.repo:
        print("--repo (or $TARGET_REPO) is required", file=sys.stderr)
        return 1

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    dry_run = is_dry_run(os.environ.get("RENOVATE_DRY_RUN"), args.dry_run)

    try:
        found = find_reapable(token, args.repo)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
        # Loud, and non-fatal to the pass: Renovate still runs after this step,
        # so a reaper outage delays recovery by one cycle rather than stopping
        # the lane. Exit 0 would make the failure invisible in the job summary.
        print(f"::warning::reaper could not inspect {args.repo}: {exc}")
        return 0

    if not found:
        print(f"{args.repo}: nothing to reap")
        return 0

    owner, name = args.repo.split("/", 1)
    for pr, reason in found:
        branch = pr["head"]["ref"]
        print(
            f"{args.repo}: PR #{pr['number']} is reapable — {reason} "
            f"({branch}, head {pr['head']['sha'][:7]}) — deleting the branch so "
            "this pass rebuilds it"
        )
        if dry_run:
            print("::notice::dry run, branch left in place")
            continue
        try:
            _request(
                token,
                f"{API_ROOT}/repos/{owner}/{name}/git/refs/heads/{branch}",
                method="DELETE",
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            # Per branch, not per repo: one lane that will not delete must not
            # stop the others from recovering on this pass.
            print(f"::warning::reaper could not delete {args.repo}@{branch}: {exc}")
            continue
        print(f"::notice::reaped {args.repo}#{pr['number']} ({branch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
