#!/usr/bin/env python3
"""Mirror semver release tags from GHCR onto Docker Hub for SDR deploys.

Self-deployed-runtime (SDR) apps pull images by their semver tag from Docker
Hub (``docker.io/atlanhq/<app>:<version>``).  The merge/build step already
pushes the full tag ladder (``:X.Y.Z``, ``:X.Y``, ``:X``, ``:latest``) to GHCR
via ``RELEASE_TAGS``, but the Docker Hub step only ``crane copy``s the
branch-sha image.  Without this script only the branch-sha tag lands on Docker
Hub, so any SDR pull of ``:<version>`` 404s with *manifest unknown*.

Each ``crane tag`` is a server-side manifest tag against the same digest that
was already pushed by the preceding ``crane copy`` — no blob re-upload.

Usage (from within a workflow step)::

    python3 .github/scripts/mirror_tags_dockerhub.py \\
        --dockerhub-image "docker.io/atlanhq/my-app:main-abc1234" \\
        --release-tags "ghcr.io/atlanhq/my-app:2.0.1\\nghcr.io/atlanhq/my-app:2.0\\n..."

Exits 0 when no ``--release-tags`` are supplied (no-op for non-release builds).

Replaces inline conditional shell that used to live in the
*Push to Docker Hub* step of ``.github/workflows/build-and-publish-app.yaml``.
See ``docs/standards/ci.md`` for why branching logic lives in scripts.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def run(cmd: list[str]) -> None:
    """Execute *cmd* and raise on non-zero exit.

    Single thin wrapper so tests can monkeypatch the external ``crane`` call
    while letting the rest of the logic run for real.
    """
    subprocess.run(cmd, check=True)


def mirror_tags(dockerhub_image: str, release_tags: str) -> list[str]:
    """Apply each tag from *release_tags* to *dockerhub_image* via ``crane tag``.

    Args:
        dockerhub_image: The Docker Hub image reference that was already pushed
            (e.g. ``docker.io/atlanhq/my-app:main-abc1234``).  The function
            derives the registry/repo prefix by stripping the tag suffix.
        release_tags: Newline-separated GHCR image references whose tags should
            be mirrored (e.g. ``ghcr.io/atlanhq/my-app:2.0.1``).  Empty string
            or whitespace-only lines are silently skipped.

    Returns:
        List of Docker Hub refs that were tagged (for logging / test assertions).
    """
    if not release_tags or not release_tags.strip():
        return []

    # Strip the tag from the DockerHub ref once — shared prefix for echo lines.
    dh_repo = dockerhub_image.rsplit(":", 1)[0]

    tagged: list[str] = []
    for ref in release_tags.splitlines():
        ref = ref.strip()
        if not ref:
            continue
        # ghcr.io/atlanhq/<app>:2.0.1  →  2.0.1
        tag = ref.rsplit(":", 1)[-1]
        run(["crane", "tag", dockerhub_image, tag])
        dest = f"{dh_repo}:{tag}"
        print(f"Tagged on DockerHub: {dest}", flush=True)
        tagged.append(dest)

    return tagged


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dockerhub-image",
        required=True,
        help="Docker Hub image ref pushed by the preceding crane copy step.",
    )
    parser.add_argument(
        "--release-tags",
        default="",
        help="Newline-separated GHCR refs whose tags should be mirrored. "
        "Empty/omitted → no-op.",
    )
    args = parser.parse_args(argv)

    mirror_tags(args.dockerhub_image, args.release_tags)


if __name__ == "__main__":
    main(sys.argv[1:])
