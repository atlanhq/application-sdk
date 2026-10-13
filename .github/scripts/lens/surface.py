"""The public names this PR takes away from the LAST RELEASE (FND-3340).

A removal is a break only if consumers could have imported the name, which
means it shipped in a release. Neither the reviewer nor the verify call can
tell that from a diff: an incremental round sees only the commits since the
last review, so a name added in round 1 and reshaped in round 3 reads as a
removal, and the verify call sees the finding's site and the round's diff,
never the release. So once per round this module asks the same question the
Symbol Removal Check asks, with the same code (`check_symbol_removals`):
which names that shipped in the newest stable tag are gone or narrowed at the
PR head?

The head tree is the base-branch checkout with the PR's changed package
modules overlaid from their head text (read through the API as data, parsed,
never imported or run). Only modules the PR touches are judged, and a name
is listed only when the head breaks it relative to BOTH the release and the
base branch: a removal the base branch already made since the release is not
this PR's doing, even in a module the PR edits.

The workflow checks out the base branch shallow and without tags. When no
release tag is local, the newest stable one is listed with `git ls-remote`
and fetched at depth 1: one small fetch, no workflow change.

It never fails the run. Whatever goes wrong becomes the block's text
("release baseline unavailable: <reason>") and the review falls back to
judging removals as it did before.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import check_symbol_removals as csr

from .diff import FileDiff

PACKAGE = csr.DEFAULT_PACKAGE
UNAVAILABLE = "release baseline unavailable"
GIT_TIMEOUT_S = 120
_STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def _git(root: Path, *args: str) -> str:
    """git with a timeout: a hung fetch must not hold the review."""
    done = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise ValueError(f"git {args[0]} failed: {done.stderr.strip()[:200]}")
    return done.stdout


def release_tag(root: Path) -> str:
    """The newest stable `vX.Y.Z` tag, fetched first when none is local.

    `csr.latest_release_tag` picks the tag; this only makes one reachable in
    a shallow, tagless checkout."""
    try:
        return csr.latest_release_tag(root)
    except ValueError:
        pass
    listed = _git(root, "ls-remote", "--tags", "--refs", "origin", "refs/tags/v*")
    stable = {}
    for line in listed.splitlines():
        name = line.rpartition("refs/tags/")[2].strip()
        m = _STABLE_TAG.match(name)
        if m:
            stable[name] = tuple(int(g) for g in m.groups())
    if not stable:
        raise ValueError("origin lists no stable v*.*.* tag")
    tag = max(stable, key=stable.__getitem__)
    _git(
        root,
        "fetch",
        "-q",
        "--no-tags",
        "--depth=1",
        "origin",
        f"+refs/tags/{tag}:refs/tags/{tag}",
    )
    return csr.latest_release_tag(root)


def _package_py(path: str | None) -> bool:
    if not path or not path.endswith(".py"):
        return False
    parts = PurePosixPath(path).parts
    return parts[0] == PACKAGE and ".." not in parts


def touched_modules(files: list[FileDiff]) -> set[str]:
    """Dotted modules the PR changes, deletes, adds or renames away from."""
    paths = {p for fd in files for p in (fd.path, fd.old_path) if _package_py(p)}
    return {csr.module_path(Path(p), Path(PACKAGE), PACKAGE) for p in paths}


def build_head_tree(
    root: Path,
    dest: Path,
    files: list[FileDiff],
    head_text: dict[str, str],
    fetch: Callable[[str], str | None],
) -> None:
    """The base checkout's package with the PR's package modules at the head."""

    def only_py(d: str, names: list[str]) -> list[str]:
        return [
            n for n in names if not n.endswith(".py") and not (Path(d) / n).is_dir()
        ]

    shutil.copytree(root / PACKAGE, dest / PACKAGE, ignore=only_py)
    at_head = {fd.path for fd in files if fd.status != "deleted"}
    for fd in files:
        if fd.old_path and fd.old_path not in at_head and _package_py(fd.old_path):
            (dest / fd.old_path).unlink(missing_ok=True)
        if not _package_py(fd.path):
            continue
        if fd.status == "deleted":
            (dest / fd.path).unlink(missing_ok=True)
            continue
        # "" in head_text may be an unreadable file (head_side_text maps a
        # failed read to ""), so an empty text is read again rather than
        # trusted: an empty module would list every name it had as removed.
        text = head_text.get(fd.path) or fetch(fd.path)
        if text is None:
            raise ValueError(f"could not read {fd.path} at the PR head")
        out = dest / fd.path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def released_surface_removals(
    root: Path,
    files: list[FileDiff],
    head_text: dict[str, str],
    fetch: Callable[[str], str | None],
) -> str:
    """The `<released_surface_removals>` text, or "" when the PR changes no
    package module (no name can be removed, so there is nothing to say).

    Lists each public name in a module the PR touches that shipped in the
    last release, is still intact on the base branch, and is removed or
    narrowed at the head, without a deprecated alias in that release. A name
    absent from the release (added earlier in the PR, or on an unreleased
    branch) is never listed, and neither is one the base branch already
    removed or narrowed: that is not this PR's doing, even when the PR edits
    the same module."""
    modules = touched_modules(files)
    if not modules:
        return ""
    try:
        tag = release_tag(root)
        release = csr.snapshot_at_ref(root, tag, PACKAGE)
        base = csr.build_snapshot(root, PACKAGE)  # the base-branch checkout
        with tempfile.TemporaryDirectory(prefix="lens-surface-") as tmp:
            build_head_tree(root, Path(tmp), files, head_text, fetch)
            head = csr.build_snapshot(Path(tmp), PACKAGE)
    except Exception as exc:  # noqa: BLE001 — advisory input: any failure falls back
        return f"{UNAVAILABLE}: {type(exc).__name__}: {str(exc)[:300]}"
    # No commit subject: a declared break is the card's own rule, judged by the
    # reviewer; here `blocking` just means "the name is public". A break is the
    # PR's only if it is one against the release AND against the base branch.
    by_pr = {f.key for f in csr.compare(base, head) if f.blocking}
    found = [
        f
        for f in csr.compare(release, head)
        if f.blocking and f.key in by_pr and f.key.split(":", 1)[0] in modules
    ]
    if not found:
        return (
            f"baseline {tag}: none (no public name that shipped in {tag} and is "
            "still on the base branch is removed or narrowed by this PR)"
        )
    lines = [
        f"removed: {f.key}"
        if f.kind == "removed"
        else f"narrowed: {f.key} — {f.detail}"
        for f in found
    ]
    return f"baseline {tag}\n" + "\n".join(lines)
