#!/usr/bin/env python3
"""Delete lock-maintenance branches whose refusal will clear itself (FND-909).

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

Safety
------
Deletes only a branch that satisfies every one of:

* the branch is exactly ``BRANCH`` (the lock-maintenance branch of the shared
  preset) — never an arbitrary or human branch,
* it has an open PR authored by the fleet app,
* the PR's only changed file is a ``uv.lock``,
* that lock's ``[options]`` table carries a refusal stamp, and
* the stamped reason is in ``SELF_HEALING_REFUSALS``.

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
# should not be widenable by a caller's typo.
BRANCH = "renovate/lock-file-maintenance"

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


def should_reap(files: list[str], lock_text: str) -> bool:
    """Is this PR a refusal that will clear itself on the next resolve?"""
    if lone_lock(files) is None:
        return False
    return refusal_reason(lock_text) in SELF_HEALING_REFUSALS


def find_refusal(token: str, repo: str, fetch: Fetch = _request) -> Optional[dict]:
    """The open lock-maintenance PR on ``repo`` if it is a self-healing refusal.

    Returns the PR payload (for logging) or None. Any transport failure raises:
    a reaper that silently does nothing on an API blip is indistinguishable from
    a healthy lane, and this script's whole purpose is to be the thing that
    notices.
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
    return pr if should_reap(files, lock_text) else None


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
        pr = find_refusal(token, args.repo)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
        # Loud, and non-fatal to the pass: Renovate still runs after this step,
        # so a reaper outage delays recovery by one cycle rather than stopping
        # the lane. Exit 0 would make the failure invisible in the job summary.
        print(f"::warning::reaper could not inspect {args.repo}: {exc}")
        return 0

    if pr is None:
        print(f"{args.repo}: no self-healing lock refusal to reap")
        return 0

    print(
        f"{args.repo}: PR #{pr['number']} is a self-healing lock refusal "
        f"({BRANCH}, head {pr['head']['sha'][:7]}) — deleting the branch so "
        "this pass rebuilds it"
    )
    if dry_run:
        print("::notice::dry run, branch left in place")
        return 0

    owner, name = args.repo.split("/", 1)
    try:
        _request(
            token,
            f"{API_ROOT}/repos/{owner}/{name}/git/refs/heads/{BRANCH}",
            method="DELETE",
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"::warning::reaper could not delete {args.repo}@{BRANCH}: {exc}")
        return 0
    print(f"::notice::reaped {args.repo}#{pr['number']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
