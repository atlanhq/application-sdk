#!/usr/bin/env python3
"""Refresh our GHCR copy of the MinIO image the Storage Emulator Tests run.

Why this exists
---------------
The S3 emulator image has been withdrawn from under us twice in one month:
MinIO stopped publishing community images to Docker Hub, then quay.io closed
anonymous pulls of ``minio/minio``. Each time the pinned tag *and* its digest
vanished everywhere, so there was nothing to fall back to. CI therefore pulls
only from a copy we own (``ghcr.io/atlanhq/ci-mirror/minio``) and never from a
vendor registry. The vendor source today is Chainguard's free MinIO image,
which may go the same way; when it does, the mirror keeps CI green and a
refresh simply stops being possible, rather than every run breaking.

This script is the refresh. It copies one Chainguard digest into the mirror,
byte-for-byte (``crane copy`` keeps the index digest and every platform), under
the MinIO release it contains, e.g. ``RELEASE.2026-09-22T19-25-18Z``. Chainguard's
free tier only publishes ``latest``, so the release tag is read from the binary
(``minio --version``) rather than from the source tag.

Tags in the mirror are immutable: an existing tag that already points at the
digest is a no-op, and one that points anywhere else is refused. Chainguard
rebuilds the same MinIO release daily, so a fresh digest for an unchanged
release is expected; repointing the tag would silently change what an existing
pin in ``sdk-tests-reusable.yaml`` means. Pins name the digest as well as the
tag, so a refused refresh never breaks CI — it only means there is nothing new
to adopt until MinIO itself cuts a release.

The script does not edit the pin. It prints the reference to paste into
``.github/workflows/sdk-tests-reusable.yaml`` (and the local-run docstrings in
``tests/integration/storage/test_emulator_*.py``), so adopting a new MinIO
release stays a reviewed change.
"""

from __future__ import annotations

import argparse
import enum
import os
import re
import subprocess
import sys
from dataclasses import dataclass

SOURCE_REPO = "cgr.dev/chainguard/minio"
MIRROR_REPO = "ghcr.io/atlanhq/ci-mirror/minio"

#: A MinIO release tag as ``minio --version`` prints it.
_RELEASE_RE = re.compile(r"RELEASE\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Registry error codes that mean "this tag does not exist yet" — as opposed to
#: an auth or network failure, which must not read as "safe to create".
_ABSENT_MARKERS = ("MANIFEST_UNKNOWN", "NAME_UNKNOWN", "manifest unknown")


class MirrorError(RuntimeError):
    """The refresh cannot proceed; the message says why and what to do."""


class Action(enum.Enum):
    COPY = "copy"
    ALREADY_MIRRORED = "already-mirrored"


@dataclass(frozen=True)
class Plan:
    action: Action
    source: str
    destination: str
    digest: str

    @property
    def pinned_ref(self) -> str:
        """The tag-plus-digest reference CI should pin."""
        return f"{self.destination}@{self.digest}"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd*, capturing output. Never raises on a non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _checked(cmd: list[str]) -> str:
    result = run(cmd)
    if result.returncode != 0:
        raise MirrorError(
            f"`{' '.join(cmd)}` failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


def validate_digest(digest: str) -> str:
    digest = digest.strip()
    if not _DIGEST_RE.match(digest):
        raise MirrorError(
            f"{digest!r} is not an image digest. Expected `sha256:` followed by "
            "64 lowercase hex characters, e.g. from `crane digest "
            f"{SOURCE_REPO}:latest`."
        )
    return digest


def parse_release_tag(version_output: str) -> str:
    """Return the single MinIO release tag in ``minio --version`` output."""
    found = list(dict.fromkeys(_RELEASE_RE.findall(version_output)))
    if len(found) != 1:
        raise MirrorError(
            f"expected exactly one MinIO release tag in `minio --version` "
            f"output, found {found or 'none'}. Output was: {version_output!r}. "
            "Without it the mirror tag cannot name the release it holds."
        )
    return found[0]


def is_absent(stderr: str) -> bool:
    """True when a failed ``crane digest`` means the tag simply does not exist."""
    return any(marker in stderr for marker in _ABSENT_MARKERS)


def plan(digest: str, release_tag: str, existing_digest: str | None) -> Plan:
    """Decide what to do, given what the mirror tag currently points at."""
    source = f"{SOURCE_REPO}@{digest}"
    destination = f"{MIRROR_REPO}:{release_tag}"
    if existing_digest is None:
        return Plan(Action.COPY, source, destination, digest)
    if existing_digest == digest:
        return Plan(Action.ALREADY_MIRRORED, source, destination, digest)
    raise MirrorError(
        f"{destination} already points at {existing_digest}, not {digest}. "
        "Mirror tags are immutable: repointing one would change what an existing "
        "pin means. Chainguard rebuilds the same MinIO release daily, so this "
        "just means there is no new MinIO release to adopt yet; keep the current "
        "pin."
    )


def resolve_source_digest(requested: str) -> str:
    if requested.strip():
        return validate_digest(requested)
    return validate_digest(_checked(["crane", "digest", f"{SOURCE_REPO}:latest"]))


def read_release_tag(digest: str) -> str:
    # The entrypoint is /usr/bin/minio, so `--version` goes straight to it.
    output = _checked(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            f"{SOURCE_REPO}@{digest}",
            "--version",
        ]
    )
    return parse_release_tag(output)


def mirror_digest_of(destination: str) -> str | None:
    result = run(["crane", "digest", destination])
    if result.returncode == 0:
        return validate_digest(result.stdout)
    if is_absent(result.stderr):
        return None
    raise MirrorError(
        f"could not read {destination} (exit {result.returncode}): "
        f"{result.stderr.strip()}. An auth failure here reads as "
        "`permission_denied`; the workflow's GITHUB_TOKEN needs write access "
        "granted on the package (Package settings -> Manage Actions access)."
    )


def execute(p: Plan, dry_run: bool) -> None:
    if p.action is Action.ALREADY_MIRRORED or dry_run:
        return
    _checked(["crane", "copy", p.source, p.destination])
    copied = mirror_digest_of(p.destination)
    if copied != p.digest:
        raise MirrorError(
            f"after `crane copy`, {p.destination} points at {copied}, not "
            f"{p.digest}. The copy did not preserve the index; do not pin it."
        )


def summary(p: Plan, dry_run: bool) -> str:
    verb = {
        Action.COPY: "Would copy" if dry_run else "Copied",
        Action.ALREADY_MIRRORED: "Already mirrored",
    }[p.action]
    return "\n".join(
        [
            "## MinIO mirror refresh",
            "",
            f"{verb}: `{p.source}` -> `{p.destination}`",
            "",
            "Pin this in `.github/workflows/sdk-tests-reusable.yaml` "
            "(`MINIO_IMAGE`) and the local-run docstrings in "
            "`tests/integration/storage/test_emulator_*.py`:",
            "",
            "```",
            p.pinned_ref,
            "```",
            "",
        ]
    )


def _append(path_var: str, text: str) -> None:
    path = os.environ.get(path_var, "")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--source-digest",
        default="",
        help=f"Digest of {SOURCE_REPO} to mirror. Empty resolves `:latest`.",
    )
    parser.add_argument(
        "--dry-run",
        choices=("true", "false"),
        default="false",
        help="Plan and report without copying.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    dry_run = args.dry_run == "true"

    try:
        digest = resolve_source_digest(args.source_digest)
        release_tag = read_release_tag(digest)
        p = plan(digest, release_tag, mirror_digest_of(f"{MIRROR_REPO}:{release_tag}"))
        execute(p, dry_run)
    except MirrorError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    text = summary(p, dry_run)
    print(text)
    _append("GITHUB_STEP_SUMMARY", text)
    _append("GITHUB_OUTPUT", f"image={p.pinned_ref}\naction={p.action.value}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
