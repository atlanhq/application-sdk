#!/usr/bin/env python3
"""Assemble the multi-arch manifest in build-and-publish-app.yaml (job ``merge``).

Former inline ``run:`` block "Create and push multi-arch manifest". Merges the
per-arch images ``<GHCR_BASE>:<BRANCH>-<IMAGE_TAG>-{amd64,arm64}`` into one
manifest tagged with the immutable and mutable branch tags, plus every tag in
``RELEASE_TAGS`` (newline-separated, empty on a non-release build).
"""

from __future__ import annotations

import sys

import _bap

ARCHES = ("amd64", "arm64")


def release_tags(raw: str) -> list[str]:
    """Non-empty lines of ``RELEASE_TAGS``, as ``while IFS= read -r`` saw them."""
    return [line for line in raw.split("\n") if line]


def create() -> int:
    ghcr_base = _bap.env("GHCR_BASE")
    ghcr_image = _bap.env("GHCR_IMAGE")
    branch_tag = _bap.env("GHCR_BRANCH_TAG")
    source = f"{ghcr_base}:{_bap.env('BRANCH')}-{_bap.env('IMAGE_TAG')}"
    ladder = release_tags(_bap.env("RELEASE_TAGS"))

    tag_args: list[str] = []
    for tag in (ghcr_image, branch_tag, *ladder):
        tag_args += ["--tag", tag]
    rc = _bap.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            *tag_args,
            *(f"{source}-{arch}" for arch in ARCHES),
        ]
    ).returncode
    if rc != 0:
        return rc

    print("Multi-arch manifest pushed:")
    print(f"  Immutable: {ghcr_image}")
    print(f"  Branch:    {branch_tag}")
    if _bap.env("RELEASE_TAGS"):
        print("  Release tags:")
        for tag in ladder:
            print(f"    {tag}")
    return 0


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(create())
