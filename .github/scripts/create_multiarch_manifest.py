#!/usr/bin/env python3
"""Combine per-architecture images into a manifest list under each target tag.

The image builds in this repo run one architecture per job, on a runner native
to it, because emulating the other half under QEMU costs 5-10x. That leaves two
single-arch images that nothing can pull by the real tag until they are joined
into a manifest list — this is the join.

``docker buildx imagetools create`` copies the source manifests (and any blobs
the target registry is missing) and writes an index referencing them. The index
bytes are a function of the child digests alone, so pushing the same sources to
two registries yields the SAME index digest in both — the cross-registry digest
parity that ``docs/standards/build-security.md`` promises for
``app-runtime-base``, and that ``resolve_base_redirect.py`` fails closed on.

Targets are grouped by repository and one ``create`` is issued per repository
with every tag of that repository attached. Grouping matters: a single call
mixing repositories would re-copy the blobs once per name, and on a
cross-registry ladder that is the whole image re-uploaded several times over.

Exits non-zero if any ``docker`` invocation fails. The push is NOT transactional
across repositories — see the recovery note in build-security.md.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import OrderedDict


def run(cmd: list) -> None:
    """Execute *cmd* and raise on non-zero exit.

    A single thin wrapper so tests can monkeypatch the external ``docker`` call
    while letting the grouping logic run for real.
    """
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def parse_tags(tags: str) -> list:
    """Split a newline-delimited tag list, dropping blank lines.

    The workflow passes this straight from a job output, which round-trips
    through a heredoc and so tends to carry a trailing newline.
    """
    return [line.strip() for line in tags.splitlines() if line.strip()]


def group_by_repo(tags: list) -> "OrderedDict[str, list]":
    """Map ``repo -> [full tag, ...]``, preserving first-seen repo order.

    A reference is ``<repo>:<tag>`` where the repo may itself contain a colon
    (a registry port, e.g. ``localhost:5000/x:1``), so the split is on the LAST
    colon — and only when it appears after the final ``/``.
    """
    grouped: "OrderedDict[str, list]" = OrderedDict()
    for ref in tags:
        repo, sep, tag = ref.rpartition(":")
        if not sep or "/" in tag:
            raise ValueError(f"not a tagged reference: {ref!r}")
        grouped.setdefault(repo, []).append(ref)
    return grouped


def create_manifests(tags: list, sources: list) -> list:
    """Issue one ``imagetools create`` per repository. Returns the commands run."""
    if not sources:
        raise ValueError("at least one --source is required")
    if not tags:
        raise ValueError("at least one tag is required")

    commands = []
    for repo, refs in group_by_repo(tags).items():
        cmd = ["docker", "buildx", "imagetools", "create"]
        for ref in refs:
            cmd += ["--tag", ref]
        cmd += list(sources)
        print(f"Creating manifest list in {repo} for {len(refs)} tag(s)")
        run(cmd)
        commands.append(cmd)
    return commands


def inspect(tags: list) -> None:
    """Print the resulting index for each tag, so the log shows both platforms."""
    for ref in tags:
        run(["docker", "buildx", "imagetools", "inspect", ref])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tags",
        required=True,
        help="Newline-delimited target references to create the manifest under.",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        required=True,
        help="A per-arch image reference. Repeat once per architecture.",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Print the resulting index for every tag after creating it.",
    )
    args = parser.parse_args()

    tags = parse_tags(args.tags)
    try:
        create_manifests(tags, args.source)
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    if args.inspect:
        inspect(tags)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
