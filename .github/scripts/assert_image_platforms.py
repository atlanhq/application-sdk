#!/usr/bin/env python3
"""Assert a pushed image's manifest actually carries every requested platform.

Why this exists
---------------
FND-31's install path needs the e2e image to be pullable by the *tenant's*
cluster, which is not the same machine as the runner. A tenant node whose
architecture is missing from the manifest does not fail the build, the publish,
or the install — LM accepts all three — and only surfaces ~2 minutes later as
``ImagePullBackOff`` with ``no matching manifest for linux/arm64`` buried in the
pod events. One live run was spent on that, and three more on misreading it.

So the check belongs at the point the image is produced, where the fix is
obvious and the feedback is seconds rather than minutes. A dropped ``--platform``
flag, a build that silently fell back to the runner's own architecture, or a
Dockerfile that cannot cross-build all become a red build step naming the missing
platform.

Input is ``docker buildx imagetools inspect --raw <image>`` — an OCI image index
or a Docker manifest list. Single-platform pushes have no index at all: the raw
output is one image manifest, which is itself the answer ("this image serves
exactly one platform"), and that must not read as "nothing to check".

Attestation manifests
---------------------
buildx attaches provenance/SBOM attestations as extra index entries carrying
``platform: {"os": "unknown", "architecture": "unknown"}``. They are not
platforms and are skipped — counting them would let an amd64-only image with two
attestations look like a four-platform index.
"""

from __future__ import annotations

import argparse
import json
import sys

#: buildx's marker for a non-platform index entry (provenance / SBOM).
_UNKNOWN = "unknown"

#: Media types whose payload is a single image manifest rather than an index.
#: Present for the error message only — the shape is detected structurally.
_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


class PlatformAssertError(RuntimeError):
    """The manifest does not carry the platforms that were asked for."""


def normalise(platform: str) -> str:
    """Return a comparable ``os/arch[/variant]`` string.

    ``linux/arm64/v8`` and ``linux/arm64`` name the same platform in every use we
    have: buildx normalises ``linux/arm64`` to variant ``v8`` on push, so a
    requested value without the variant would never match the manifest's. The
    variant is dropped on both sides rather than defaulted on one, which keeps
    ``linux/arm/v7`` (where the variant IS the distinction) intact — that case
    differs in the *architecture* field too (``arm`` vs ``arm64``).
    """
    parts = [p.strip() for p in platform.strip().split("/") if p.strip()]
    if len(parts) < 2:
        raise PlatformAssertError(
            f"{platform!r} is not a platform. Expected `os/arch`, e.g. "
            "`linux/amd64` or `linux/arm64`."
        )
    if parts[1] == "arm64":
        return f"{parts[0]}/{parts[1]}"
    return "/".join(parts[:3])


def _entry_platform(entry: object) -> str:
    """Return one index entry's platform, or ``""`` if it is not a platform."""
    if not isinstance(entry, dict):
        return ""
    platform = entry.get("platform")
    if not isinstance(platform, dict):
        return ""
    os_name = str(platform.get("os", "")).strip()
    arch = str(platform.get("architecture", "")).strip()
    if not os_name or not arch or _UNKNOWN in (os_name, arch):
        return ""
    variant = str(platform.get("variant", "")).strip()
    return normalise("/".join(p for p in (os_name, arch, variant) if p))


def platforms_in(raw: str) -> list[str]:
    """Return the platforms a raw ``imagetools inspect`` payload serves.

    An index yields one per non-attestation entry. A bare image manifest yields
    nothing: a single manifest carries no platform field (the platform lives in
    its config blob, which is a second fetch away), so the caller decides what an
    empty result means rather than this function guessing.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlatformAssertError(
            f"could not parse the manifest as JSON ({exc}). Expected the output "
            "of `docker buildx imagetools inspect --raw <image>`."
        ) from exc
    if not isinstance(parsed, dict):
        raise PlatformAssertError(
            f"expected a JSON object, got {type(parsed).__name__}."
        )

    manifests = parsed.get("manifests")
    if not isinstance(manifests, list):
        return []
    found = [_entry_platform(entry) for entry in manifests]
    # dict.fromkeys: de-duplicate while keeping the manifest's own order, so the
    # error message lists platforms as the registry does.
    return list(dict.fromkeys(p for p in found if p))


def assert_platforms(raw: str, expected: str) -> list[str]:
    """Raise unless every platform in *expected* is served by the manifest."""
    wanted = [normalise(p) for p in expected.split(",") if p.strip()]
    if not wanted:
        raise PlatformAssertError(
            "no expected platforms given. Pass --expected linux/amd64,linux/arm64 "
            "(an empty value would make this check silently pass)."
        )

    found = platforms_in(raw)
    if not found:
        raise PlatformAssertError(
            f"the pushed image is a single-platform manifest, not a multi-platform "
            f"index, so it cannot serve {', '.join(wanted)}. The `--platform` flag "
            "was dropped or the build fell back to the runner's own architecture. "
            "A tenant node on a missing architecture accepts the publish and the "
            "install, then fails minutes later as ImagePullBackOff."
        )

    missing = [p for p in wanted if p not in found]
    if missing:
        raise PlatformAssertError(
            f"the pushed image serves {', '.join(found)} but not "
            f"{', '.join(missing)}. Nothing downstream catches this: GM accepts "
            "the version, LM accepts the install, and the tenant's cluster fails "
            "the pull minutes later with `no matching manifest`."
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected",
        required=True,
        help=(
            "Comma-separated platforms the image must serve, e.g. "
            "linux/amd64,linux/arm64."
        ),
    )
    parser.add_argument(
        "--manifest-file",
        default="",
        help=(
            "File holding `docker buildx imagetools inspect --raw` output. "
            "Reads stdin when unset, which is the pipeline form the action uses."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    raw = (
        open(args.manifest_file, encoding="utf-8").read()  # noqa: SIM115
        if args.manifest_file
        else sys.stdin.read()
    )
    try:
        served = assert_platforms(raw, args.expected)
    except PlatformAssertError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(f"image serves: {', '.join(served)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
