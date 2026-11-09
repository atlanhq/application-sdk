#!/usr/bin/env python3
"""Prepare-job steps of build-and-publish-app.yaml (job ``prepare``).

Subcommands (each one is a former inline ``run:`` block):

``sdr-secrets`` / ``publish-secrets``
    Fail when a secret the run needs is missing. The workflow passes only
    whether it is set (``HAS_PAT`` / ``HAS_TOKEN`` = ``true``/``false``), never
    its value.

``app-configs``
    Collect the connector form JSON files under ``app/generated/`` into one
    base64-encoded JSON blob (output ``app_configs``). Base64 keeps the value
    inert to Kong's XSS plugin on the publish route; GM decodes it.

``compute-tags``
    Derive the branch slug, image tags, release tag ladder and build identity
    from ``REF``, ``REPO``, ``RELEASE_TAG`` and the checked-out HEAD.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import secrets
import sys
from pathlib import Path

import _bap

_REF = re.compile(r"(refs/heads/|refs/tags/)?[a-zA-Z0-9/_.-]+")
_RELEASE_TAG = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?")


def _require(flag_env: str, message: str) -> int:
    if _bap.env(flag_env) != "true":
        print(f"::error::{message}")
        return 1
    return 0


def sdr_secrets() -> int:
    return _require(
        "HAS_PAT",
        "atlan.yaml sets self_deployed_runtime=true but DOCKER_HUB_PAT_RW secret is "
        "not set in this repo.",
    )


def publish_secrets() -> int:
    return _require(
        "HAS_TOKEN",
        "publish=true but APP_PUBLISH_TOKEN secret is not set in this repo.",
    )


def app_configs(generated: str = "app/generated") -> int:
    generated_dir = Path(generated)
    form_files: dict[str, object] = {}
    if generated_dir.is_dir():
        for f in sorted(generated_dir.rglob("*.json")):
            try:
                form_files[f.name] = json.loads(f.read_text())
            except json.JSONDecodeError as e:
                # Block the publish: a dropped file would ship an app_configs
                # missing a connector form, which breaks installs on tenants.
                print(f"::error file={f}::Invalid JSON in connector form: {e}")
                return 1
    value = (
        base64.b64encode(json.dumps(form_files).encode("utf-8")).decode("ascii")
        if form_files
        else ""
    )
    print(f"[app_configs] found {len(form_files)} files: {list(form_files.keys())}")
    _bap.output(app_configs=value)
    return 0


def release_tag_ladder(ghcr_base: str, release_tag: str, image_tag: str) -> list[str]:
    """GHCR tags a release pushes: the full ladder for stable, version-only for pre."""
    version = release_tag[1:] if release_tag.startswith("v") else release_tag
    parts = version.split(".")
    major, minor = parts[0], ".".join(parts[:2])
    if "-" not in version:
        return [
            f"{ghcr_base}:latest",
            f"{ghcr_base}:{version}",
            f"{ghcr_base}:{minor}",
            f"{ghcr_base}:{major}",
            f"{ghcr_base}:sha-{image_tag}",
        ]
    return [f"{ghcr_base}:{version}", f"{ghcr_base}:sha-{image_tag}"]


def compute_tags() -> int:
    repo = _bap.env("REPO")
    ref = _bap.env("REF")
    # The checked-out HEAD, not github.sha: github.sha is the trigger branch,
    # which differs from the `ref` input on an entrypoint_dispatch.
    sha = _bap.run_checked(["git", "rev-parse", "HEAD"])
    if not _REF.fullmatch(ref):
        print(f"::error::Invalid ref format: {ref}")
        return 1

    branch = (
        ref.replace("refs/heads/", "", 1).lower().replace("/", "-").replace("_", "-")
    )
    # A release build arrives as refs/tags/v*; name its tags after main, which
    # tag-and-release.yaml cut it from, rather than `refs-tags-v0.3.0`.
    release_tag = _bap.strip_crlf(_bap.env("RELEASE_TAG"))
    if release_tag and ref == f"refs/tags/{release_tag}":
        branch = "main"

    image_tag = sha[:7]
    ghcr_base = f"ghcr.io/atlanhq/{repo}"
    ghcr_image = f"{ghcr_base}:{branch}-{image_tag}"
    ghcr_branch_tag = f"{ghcr_base}:{branch}"
    dockerhub_image = f"atlanhq/{repo}:{branch}-{image_tag}"
    _bap.output(
        branch=branch,
        image_tag=image_tag,
        ghcr_base=ghcr_base,
        ghcr_image=ghcr_image,
        ghcr_branch_tag=ghcr_branch_tag,
        dockerhub_image=dockerhub_image,
    )

    release_tags = ""
    if release_tag:
        if not _RELEASE_TAG.fullmatch(release_tag):
            print(f"::error::Invalid release_tag format: '{release_tag}'")
            return 1
        release_tags = "\n".join(release_tag_ladder(ghcr_base, release_tag, image_tag))
    delim = secrets.token_hex(8)
    _bap.append("GITHUB_OUTPUT", f"release_tags<<{delim}\n{release_tags}\n{delim}\n")

    # The identity the image carries (app/atlan_build.json) and publish sends
    # as `version`: the release tag for semver apps, the sha7 for CD apps.
    gm_version = release_tag or image_tag
    _bap.output(gm_version=gm_version, commit_sha=sha)

    print(f"Branch: {branch}")
    print(f"GM version: {gm_version}")
    print(f"Commit: {sha}")
    print(f"Immutable image: {ghcr_image}")
    print(f"Mutable branch tag: {ghcr_branch_tag}")
    print(f"DockerHub image: {dockerhub_image}")
    if release_tags:
        print(f"Release tags:\n{release_tags}")
    return 0


COMMANDS = {
    "sdr-secrets": sdr_secrets,
    "publish-secrets": publish_secrets,
    "app-configs": app_configs,
    "compute-tags": compute_tags,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=tuple(COMMANDS))
    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command]()
    except _bap.CommandFailed as exc:
        return exc.returncode


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(main())
