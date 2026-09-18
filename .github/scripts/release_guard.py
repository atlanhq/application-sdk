"""
release_guard.py
----------------
Shared guards deciding whether a release-opener run should act at all. Two
independent questions, each answered against the target branch fetched fresh
rather than against the checkout the job started from:

1. Did the pull request that fired this run actually land on the target
   branch? (``landed_on_branch``)
2. Has the version this run computed already been released?
   (``already_released``)

Either answer being "no"/"yes" means the caller sets ``skip=true`` and every
mutating step stays gated off.

Why the ancestry check exists (application-sdk#3794, 2026-09-17)
================================================================
GitHub's stacked pull requests deliver a child PR's merge into its **parent
feature branch** as a ``pull_request: closed`` event on the stack's *root*:
``github.ref`` is ``refs/heads/main``, so a ``branches: [main]`` filter
matches, while ``github.sha`` is the parent feature branch's new tip.
actions/checkout then fetches that sha *as* ``origin/main``.

===================  =========================================================
06:01:08             #3753 (``feat/oom-restart-action``) merges into its parent
                     ``feat/dirty-restart-marker``. Nothing reaches main.
06:01:11             Every ``branches: [main]`` closed-PR workflow fires,
                     ``release.yaml`` among them, with ``TARGET_BRANCH=main``
06:01:24             Checkout: ``git checkout -B main refs/remotes/origin/main``
                     — but that ref now points at the feature branch tip
06:01:34             Reads version 3.34.2 (main was at 3.34.3), last tag
                     ``v3.34.2``, computes 3.35.0
06:01:40             Force-pushes ``bump-version-main``: 15 unmerged feature
                     commits plus the bump. PR #3794 goes CONFLICTING.
===================  =========================================================

The version check below **cannot** catch this: 3.35.0 was genuinely newer than
anything on main, so ``already_released`` correctly said proceed. The only
signal that distinguishes "merged into main" from "merged into a sibling
branch GitHub is reporting as main" is ancestry — the commit the event names
(``github.event.pull_request.merge_commit_sha``) must be reachable from the
target branch's tip. That is what ``landed_on_branch`` asks git.

Why the version check exists (application-sdk#3570, 2026-08-31)
===============================================================
Every release lane in this repo fires on ``pull_request: closed`` and checks
out the PR's merge ref. That ref is **frozen** at the moment GitHub computed
it, so a run triggered by PR *B* can be looking at a tree that predates the
merge of release PR *A* — even though *A* landed on main seconds earlier and
its package was already published.

Observed on 2026-08-31 (application-sdk#3570):

===================  =========================================================
22:25:15             ``c532fd16`` — the v0.25.0 release PR (#3443) lands on main
22:27:55             Publish run starts for v0.25.0
22:27:56             Release opener starts for a *sibling* PR (#3569)
22:28:02–07          Checks out ``refs/pull/3569/merge`` — parent ``da1ed376``,
                     i.e. main **before** the release merge
22:28:10             Reads version 0.24.0, mints 0.25.0 a second time
22:28:17             Force-pushes the shared bump branch, opens a duplicate PR
22:28:19             Publish creates tag ``conformance-v0.25.0``
===================  =========================================================

Merging that duplicate would have attempted a second PyPI upload of 0.25.0 and
a second ``conformance-v0.25.0`` tag.

Why the check is version-based and not tag-based
================================================
The obvious guard — "skip if the tag for the version I computed already
exists" — **does not work**, and the timeline above is the proof: the tag was
created at 22:28:19, nine seconds *after* the opener read the repo at
22:28:10. At the only moment the guard could have run, the tag did not exist.

What *was* already true at 22:28:10 is that ``origin/main`` carried version
0.25.0 (merged at 22:25:15). So the reliable signal is the version on the
target branch, read fresh from the remote rather than from the frozen
checkout. Please do not "simplify" this into a tag-existence check.

Fail-open by design
===================
Every failure to determine the remote version — no network, no token scope,
missing file, a version string this module cannot parse — returns "not already
released" and lets the release proceed. Likewise every failure to *test*
ancestry — no merge commit supplied (``workflow_dispatch``), a failed fetch, a
git error other than a definite "not an ancestor" — returns "landed". A lane
that goes red because it could not reach origin would be a worse defect than
the duplicate or mis-based PR these guards prevent.
"""

from __future__ import annotations

import re
import subprocess

VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
TRIPLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Environment variable through which the workflows hand the opener scripts the
# commit the closed PR produced (``github.event.pull_request.merge_commit_sha``).
# An env var rather than a CLI flag so that an app repo pinned to an older
# ``sdk_scripts_ref`` — whose release.py does not know about it — keeps working
# unchanged when it calls the current reusable workflow.
MERGE_COMMIT_ENV = "PR_MERGE_COMMIT_SHA"


def parse_version(text):
    """Return the ``version = "..."`` value in *text*, or None.

    Matches both ``pyproject.toml`` and Pkl ``PklProject`` files, which
    happen to share this spelling.
    """
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None


def version_tuple(version):
    """Return (major, minor, patch) for a plain X.Y.Z string, else None.

    Anything with a pre-release or build suffix returns None, which makes the
    caller fail open. That is deliberate: ordering ``1.0.0-rc1`` against
    ``1.0.0`` correctly needs full PEP 440 / semver semantics, and this module
    is stdlib-only so it can run in every lane (the conformance and
    contract-toolkit openers install no extra packages).
    """
    m = TRIPLE_RE.match(version or "")
    return tuple(int(x) for x in m.groups()) if m else None


def landed_on_branch(sha, branch="main", remote="origin"):
    """Return ``(landed, detail)``: did *sha* actually reach ``<remote>/<branch>``?

    *sha* is the commit the closing pull request produced. For a PR merged into
    the target branch it is reachable from that branch's tip. For a stacked PR
    merged into its parent feature branch it is not — even though GitHub
    delivers the event as if it had targeted the stack's root (see the module
    docstring).

    The branch tip is fetched fresh with a *forced* refspec. actions/checkout
    has just pointed ``refs/remotes/<remote>/<branch>`` at ``github.sha`` — the
    very commit whose ancestry is in question — so a plain fetch would be
    refused as non-fast-forward in exactly the case that matters.

    Fail-open: an empty *sha*, a failed fetch, or any git exit other than
    ``merge-base --is-ancestor``'s definitive 1 all return ``True``. *detail*
    is a one-line explanation for the run log.
    """
    sha = (sha or "").strip()
    if not sha:
        return True, "no merge commit supplied; ancestry not checked"

    tracking = f"refs/remotes/{remote}/{branch}"
    fetched = subprocess.run(
        ["git", "fetch", "--quiet", remote, f"+{branch}:{tracking}"],
        capture_output=True,
        text=True,
    )
    if fetched.returncode != 0:
        return True, f"could not fetch {remote}/{branch}; ancestry not checked"

    probe = subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, tracking],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return True, f"{sha[:12]} is on {remote}/{branch}"
    if probe.returncode == 1:
        return False, f"{sha[:12]} is not reachable from {remote}/{branch}"
    return True, (
        f"git merge-base exited {probe.returncode} for {sha[:12]}; "
        "ancestry not checked"
    )


def not_landed_message(sha, branch="main"):
    """Human-readable reason, for the run log."""
    return (
        f"The commit this run is reacting to, {sha[:12]}, did not land on "
        f"origin/{branch}. GitHub reports a stacked pull request's merge into "
        f"its parent branch as a closed PR on the stack's root, so this run is "
        f"looking at a feature branch, not at {branch}. Bumping from here would "
        f"build a release on unmerged code (application-sdk#3794). Skipping."
    )


def version_on_branch(path, branch="main", remote="origin"):
    """Version of *path* as it exists on ``<remote>/<branch>``, or None.

    Fetches the branch first so the answer reflects the remote *now*, not the
    frozen merge-ref checkout the job started from.
    """
    # Fetch into the remote-tracking ref explicitly. Reading FETCH_HEAD instead
    # would be wrong: it is mutable global state in the checkout, so any other
    # git fetch earlier in the job (the private-dependency auth steps in the
    # app lane do run git commands) leaves its own value behind.
    fetched = subprocess.run(
        ["git", "fetch", "--quiet", remote, f"{branch}:refs/remotes/{remote}/{branch}"],
        capture_output=True,
        text=True,
    )
    if fetched.returncode != 0:
        # A non-fast-forward update of the tracking ref is fine to ignore; the
        # plain fetch below still refreshes it in the common case.
        subprocess.run(
            ["git", "fetch", "--quiet", remote, branch],
            capture_output=True,
            text=True,
        )

    for ref in (f"{remote}/{branch}", "FETCH_HEAD"):
        shown = subprocess.run(
            ["git", "show", f"{ref}:{path}"], capture_output=True, text=True
        )
        if shown.returncode == 0:
            parsed = parse_version(shown.stdout)
            if parsed:
                return parsed
    return None


def already_released(path, new_version, branch="main", remote="origin"):
    """Return ``(skip, remote_version)`` for the release about to be minted.

    *skip* is True when ``<remote>/<branch>`` already carries *new_version* or
    newer — meaning some other run has already opened and merged this release,
    so minting it again would duplicate a published version.

    Returns ``(False, ...)`` — let the release proceed — whenever the remote
    version cannot be determined or compared. *remote_version* is returned
    alongside so the caller can log the evidence without a second fetch.
    """
    remote_version = version_on_branch(path, branch=branch, remote=remote)
    remote_t = version_tuple(remote_version)
    new_t = version_tuple(new_version)
    if remote_t is None or new_t is None:
        return False, remote_version
    return remote_t >= new_t, remote_version


def skip_message(path, new_version, remote_version, branch="main"):
    """Human-readable reason, for the run log."""
    return (
        f"{path} on origin/{branch} is already at {remote_version!r}, which is "
        f">= the computed {new_version!r}. This release has already been "
        f"published — most likely this run checked out a frozen merge ref that "
        f"predates it. Skipping instead of opening a duplicate release PR."
    )
