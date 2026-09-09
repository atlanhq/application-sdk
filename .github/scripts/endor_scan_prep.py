#!/usr/bin/env python3
"""Prepare an Endor Labs container scan: credential gate and image tarball.

Two decisions that ``build-and-scan.yaml`` (the fleet's reusable PR scan) and
``daily-security-scan.yml`` (the hourly base-image scan) used to make in inlined
``if``/``else`` shell, moved here per docs/standards/ci.md so every branch is
pytest-covered (tests/test_endor_scan_prep.py).

``credentials``
    Endor is opt-in per org secret. Reports ``configured=true|false`` on
    ``$GITHUB_OUTPUT`` from the *presence* of ``ENDOR_KEY`` so the scan steps
    can skip cleanly; without this, every fork PR and every repo not yet
    licensed would show a red (if ignored) job. The value is never read beyond
    an emptiness check and never printed.

``materialise``
    Normalise both image sources onto one tarball path for ``endorctl``:

    * ``PREBUILT`` set (a published ref, or the base image): ``docker pull``
      then ``docker save`` it to ``TARBALL``. Both carry an explicit
      ``--platform linux/amd64``. Docker 29 rejects ``docker save`` on a
      multi-arch reference without one ("both OS and Architecture must be
      provided"), and endorctl shells out to ``docker save`` itself.
    * ``PREBUILT`` empty (the PR path): the build job's buildx
      ``outputs: type=docker,dest=/tmp/image.tar`` already produced the
      tarball and the artifact download placed it at ``TARBALL``; nothing to
      pull. The build tagged the local image ``scan-target:<sha>``, which is
      meaningless in the Endor UI, so the scan is reported under the name the
      image will be published as (``ghcr.io/atlanhq/<repo>:<sha7>``) and PR
      scans line up with published images on one container.

    Emits ``ref=<image reference>`` on ``$GITHUB_OUTPUT``. A missing tarball is
    a loud ``::error::`` here rather than a cryptic endorctl failure later.

Every input arrives as an environment variable and is only ever handled as
data. ``REF`` in particular is caller-supplied (``inputs.ref``) on a reusable
workflow; the previous inline form interpolated it into the ``run:`` body,
where a value carrying ``$(...)`` or a backtick would have executed as shell.
Docker is invoked with an argument list, never a shell string.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

RunFn = Callable[[list[str]], None]

PLATFORM = "linux/amd64"
GHCR_ORG = "ghcr.io/atlanhq"
SHORT_SHA_LEN = 7
DEFAULT_TARBALL = "/tmp/image.tar"

SKIP_NOTICE = (
    "::notice title=Endor scan skipped::ENDOR_API_CREDENTIALS_KEY is not "
    "available to this run."
)


def _run(cmd: list[str]) -> None:
    """Testability seam: the only place a subprocess is spawned."""
    subprocess.run(cmd, check=True)


def _write_output(key: str, value: str) -> None:
    """Append ``key=value`` to ``$GITHUB_OUTPUT``, or print it outside Actions."""
    target = os.environ.get("GITHUB_OUTPUT")
    line = f"{key}={value}"
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    else:
        print(line)


def credentials_configured(key: str | None) -> bool:
    """True when the Endor API key is present. Whitespace-only counts as absent."""
    return bool(key and key.strip())


def local_ref(repo: str, ref: str) -> str:
    """The published-image name a locally built PR image is reported under."""
    if not repo.strip():
        raise ValueError("REPO is empty; cannot name the local image for Endor")
    if not ref.strip():
        raise ValueError("REF is empty; cannot derive the image tag for Endor")
    return f"{GHCR_ORG}/{repo.strip()}:{ref.strip()[:SHORT_SHA_LEN]}"


def plan_materialise(
    prebuilt: str, repo: str, ref: str, tarball: str
) -> tuple[list[list[str]], str]:
    """Return ``(docker commands, image reference)`` for the given inputs.

    Pure function of its inputs so both branches are pytest-covered; the
    caller runs the commands through the ``_run`` seam.
    """
    prebuilt = prebuilt.strip()
    if prebuilt:
        return (
            [
                ["docker", "pull", "--platform", PLATFORM, prebuilt],
                ["docker", "save", "--platform", PLATFORM, "-o", tarball, prebuilt],
            ],
            prebuilt,
        )
    return [], local_ref(repo, ref)


def run_credentials(env: dict[str, str]) -> int:
    configured = credentials_configured(env.get("ENDOR_KEY"))
    _write_output("configured", "true" if configured else "false")
    if not configured:
        print(SKIP_NOTICE)
    return 0


def run_materialise(env: dict[str, str], run: RunFn = _run) -> int:
    tarball = env.get("TARBALL", "").strip() or DEFAULT_TARBALL
    try:
        commands, ref = plan_materialise(
            env.get("PREBUILT", ""), env.get("REPO", ""), env.get("REF", ""), tarball
        )
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    for cmd in commands:
        run(cmd)
    if not Path(tarball).is_file():
        print(
            f"::error::no image tarball at {tarball}. On the PR path the "
            "`docker-image*` artifact download must have placed it there; on the "
            "prebuilt path `docker save` should have written it.",
            file=sys.stderr,
        )
        return 1
    _write_output("ref", ref)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "credentials", help="report configured=true|false from ENDOR_KEY presence"
    )
    sub.add_parser(
        "materialise",
        help="pull+save PREBUILT, or name the downloaded tarball; report ref=",
    )
    args = parser.parse_args(argv)
    env = dict(os.environ)
    if args.command == "credentials":
        return run_credentials(env)
    return run_materialise(env)


if __name__ == "__main__":
    raise SystemExit(main())
