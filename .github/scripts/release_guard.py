"""
release_guard.py
----------------
Shared guard against a release opener minting a version that has *already*
been released.

Why this exists
===============
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
released" and lets the release proceed. A lane that goes red because it could
not reach origin would be a worse defect than the duplicate PR this prevents.
"""

from __future__ import annotations

import re
import subprocess

VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
TRIPLE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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
