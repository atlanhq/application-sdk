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
from .tools import secret_diff

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
    "**/*.jpeg",
    "**/*.gif",
    "**/*.ico",
    "**/*.webp",
    "**/*.snap",
    "**/snapshots/**",
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


def excluded(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Whether `path` matches one of the exclude patterns, as `select_files` reads them."""
    return _match(path, patterns)


def model_visible(
    files: list[FileDiff], exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE
) -> list[FileDiff]:
    """The changed files whose diff a prompt may carry: never a change that may
    hold secrets (either side of a rename), whatever `exclude` says, and never
    a file the exclude patterns name.

    Selection already applies both to the line review. Every other prompt that
    shows a diff (the approach check, verify, the base-branch check of a new
    finding) gets its files from a wider list (the whole PR, the round's range)
    and must pass it through here first."""
    return [fd for fd in files if not secret_diff(fd) and not _match(fd.path, exclude)]


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
        elif secret_diff(fd):
            # Before any other rule: its diff is never sent to the model. The
            # tools refuse to read such a file, and a review must not hand over
            # in a prompt what a tool call would be refused.
            skipped.append((fd.path, "may hold secrets: not sent to the model"))
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
