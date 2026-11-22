"""Which changed files are reviewed at all — decided in code, before any model call.

A file excluded here costs nothing. Generated artefacts, lockfiles, vendored
trees and binaries are never worth a model's attention; deletion-only changes
have nothing on the right side to comment on. Each exclusion carries a
reason so the summary can say what was skipped instead of silently not
looking.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from .diff import FileDiff

DEFAULT_EXCLUDE = (
    "uv.lock",
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "CHANGELOG.md",
    "**/generated/**",
    "**/_generated/**",
    "**/*.min.js",
    "**/*.svg",
    "**/*.png",
    "**/*.jpg",
    "**/*.snap",
    "**/snapshots/**",
    "docs/agents/sdk-capabilities.md",
    ".worktrees/**",
)


@dataclass
class Selection:
    reviewed: list[FileDiff]
    skipped: list[tuple[str, str]]  # (path, reason)


def _match(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(
        fnmatch.fnmatch(path, p) or fnmatch.fnmatch("/" + path, "*/" + p.lstrip("*/"))
        for p in patterns
    )


def select_files(
    files: list[FileDiff],
    *,
    exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE,
    max_changed_lines_per_file: int = 1500,
) -> Selection:
    reviewed: list[FileDiff] = []
    skipped: list[tuple[str, str]] = []
    for fd in files:
        if fd.is_binary:
            skipped.append((fd.path, "binary"))
        elif fd.status == "deleted" or not fd.added_lines and not fd.commentable_lines:
            skipped.append((fd.path, "deletion only"))
        elif _match(fd.path, exclude):
            skipped.append((fd.path, "excluded by pattern"))
        elif fd.additions + fd.deletions > max_changed_lines_per_file:
            skipped.append(
                (fd.path, f"over {max_changed_lines_per_file} changed lines")
            )
        elif not fd.added_lines:
            skipped.append((fd.path, "no added lines"))
        else:
            reviewed.append(fd)
    return Selection(reviewed=reviewed, skipped=skipped)
