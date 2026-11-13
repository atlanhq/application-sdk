#!/usr/bin/env python3
"""Channel gates and the Global Marketplace publish for build-and-publish-app.yaml.

Subcommands (each one is a former inline ``run:`` block):

``channel-gate``
    Job ``validate-channel``. A channel='all' release reaches every production
    tenant, so it must be built from ``main``. Reads ``REF``, ``CHANNEL``,
    ``TENANTS``, ``RELEASE_TAG``.

``publish``
    Job ``publish``, step "Publish version". Applies the semver-release gate,
    builds the ``POST /api/service/marketplace/publish`` body, sends it, and
    writes the run summary.

The body itself comes from ``marketplace_publish_body.build`` — the same
builder the e2e path registers its per-PR versions with, so the two cannot send
different shapes. This module only maps the publish job's env onto a
``PublishRequest`` and resolves the release-specific ``version`` and ``image``.
The golden bodies in ``tests/test_build_and_publish_publish.py`` pin the exact
bytes every app release sends.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import _bap

# .github/scripts, which holds marketplace_publish_body.py (stdlib only). The
# publish job's sparse checkout of .github/scripts brings it along.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from marketplace_publish_body import (  # noqa: E402
    PublishBodyError,
    PublishRequest,
    build,
)

PUBLISH_PATH = "/api/service/marketplace/publish"


def _effective_channel(channel: str) -> str:
    # ``${CHANNEL:-all}`` then ``${X,,}``: an empty channel means all, and a
    # stray 'ALL' must not slip past a case-sensitive comparison.
    return (channel or "all").lower()


def tenant_ids(tenants: str) -> tuple[str, ...]:
    """The tenant IDs in a comma-separated ``tenants`` input, blanks dropped."""
    return tuple(t.strip() for t in tenants.split(",") if t.strip())


def _refuse_tenants_without_ids(tenants: str) -> bool:
    """Fail a ``tenants`` input that is set but names no tenant (``","``).

    Every decision here keys off the parsed IDs, and with none the body falls
    back to ``target_channel`` — ``all`` when no channel is given. So an input
    like ``","`` would read as "targeted" to a human and publish to every tenant.
    Refusing it is the only reading that cannot widen a publish.
    """
    if tenants.strip() and not tenant_ids(tenants):
        print(
            "::error title=Publish blocked::tenants is set but names no tenant "
            f"({tenants!r}). Pass comma-separated tenant IDs, or leave it empty "
            "to publish by channel."
        )
        return True
    return False


# ── channel-gate ──────────────────────────────────────────────────────────────


def channel_gate() -> int:
    ref = _bap.env("REF")
    tenants = _bap.env("TENANTS")
    release_tag = _bap.strip_crlf(_bap.env("RELEASE_TAG"))

    if _refuse_tenants_without_ids(tenants):
        return 1

    # Bypass only when the ref really is the matching tag ref: a non-empty
    # release_tag alone could be passed from any feature branch.
    if release_tag and ref == f"refs/tags/{release_tag}":
        print(f"Tagged release ({release_tag}) — main-branch gate skipped.")
        return 0

    # Mirrors the channel resolution at publish time:
    #   tenants -> specific (skip), ''/'all' -> all (enforce), other -> skip.
    if tenant_ids(tenants):
        print(f"Tenants targeting ({tenants}) — main-branch gate skipped.")
        return 0
    channel = _effective_channel(_bap.env("CHANNEL"))
    if channel != "all":
        print(f"Channel '{channel}' — main-branch gate skipped.")
        return 0

    # Short and qualified main only; feature branches and bare tags (without a
    # matching release_tag, handled above) are rejected.
    if ref in ("main", "refs/heads/main"):
        print("Channel 'all' on main — proceeding.")
        return 0

    print(
        "::error title=Publish blocked::Channel 'all' requires the main branch "
        f"— got ref '{ref}'."
    )
    _bap.summary(
        "### Publish blocked",
        "",
        "Releases to the **all** channel must be built from the **main** branch.",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Ref | `{ref}` |",
        f"| Channel | `{channel}` |",
        "",
        "Re-run with one of:",
        "- `channel: beta` or `channel: staging` (non-prod)",
        "- `tenants: <comma-separated-ids>` (targeted to specific tenants)",
        "- merge to `main` and let the push-trigger run",
    )
    return 1


# ── publish ───────────────────────────────────────────────────────────────────


def build_body(environ: dict[str, str]) -> dict[str, object]:
    """The publish body, in the shape the atlan CLI's registerInMarketplace sends."""
    # Prefer release_tag (e.g. v0.3.0) over commit SHA for the GM version.
    # Backward compatible: apps that don't pass release_tag get SHA as before.
    release_tag = environ.get("RELEASE_TAG", "").strip()
    # Same string the build job baked into the image (app/atlan_build.json),
    # so the worker's self-reported app_version equals versions.version.
    version = environ.get("GM_VERSION", "").strip() or (
        release_tag if release_tag else environ["IMAGE_TAG"]
    )

    # Semver apps send the plain version tag (e.g. :0.3.0) as the image so the
    # GM's semver gate accepts it. The release ladder strips the leading 'v'
    # (v0.3.0 → 0.3.0), so do the same here to reference a tag that exists in
    # GHCR. CD apps always send the immutable branch-sha tag.
    release_model = environ.get("RELEASE_MODEL", "").strip()
    ghcr_image = environ["GHCR_IMAGE"]
    if release_tag and release_model == "semver":
        image_base = ghcr_image.rsplit(":", 1)[0]
        version_tag = release_tag.lstrip("v")
        image = f"{image_base}:{version_tag}"
    else:
        image = ghcr_image

    # Absent → all, as `${CHANNEL:-all}` did. Deliberately not lowered: GM owns
    # channel names, and the gate above is the only place case is folded.
    channel = environ.get("CHANNEL", "all") or "all"
    tenants = environ.get("TENANTS", "")
    allowed_tenants = tenant_ids(tenants)
    # Set-but-empty must never fall through to the channel (see
    # _refuse_tenants_without_ids); publish() refuses it first, and this keeps
    # the builder from accepting it when called on its own.
    if tenants.strip() and not allowed_tenants:
        raise PublishBodyError(f"tenants is set but names no tenant ({tenants!r}).")
    request = PublishRequest(
        app_id=environ["APP_ID"],
        image=image,
        version=version,
        branch=environ["BRANCH"],
        # Always set by the workflow (server_url/repository), so always sent.
        repo_url=environ["REPO_URL"],
        allowed_tenants=allowed_tenants,
        target_channel=channel,
        deploy_config=environ.get("DEPLOY_CONFIG", ""),
        # SDR capability rides inside the opaque config YAML — GM stores
        # versions.config as raw TEXT and echoes it back in the tenant catalog,
        # so LM can gate "Install on more environments" per app without a GM
        # schema change. Only injected when true: absence means not SDR-capable
        # (keeps non-SDR configs byte-identical).
        self_deployed_runtime=environ.get("ENABLE_SDR", "").strip() == "true",
        sdk_version=environ.get("SDK_VERSION", ""),
        # Native multi-package declarations from atlan.yaml. Absent → GM falls
        # back to App.argo_package_names for entrypoint expansion.
        entrypoints=environ.get("ENTRYPOINTS", ""),
        # Base64-encoded JSON blob of connector form files (see prepare job).
        # GM's VersionBody validator decodes this back to the JSON string.
        app_configs=environ.get("APP_CONFIGS", ""),
        release_model=release_model,
        created_by=environ.get("CREATED_BY", ""),
        # Full git SHA the image was built from. GM stores it on the version so
        # a worker's baked commit_sha resolves to a release even for semver
        # apps, whose `version`/`image` carry no SHA.
        commit_sha=environ.get("COMMIT_SHA", ""),
    )
    return build(request)


def publish() -> int:
    environ = dict(os.environ)
    # Strip CR/LF that GitHub expression evaluation can inject.
    release_tag = _bap.strip_crlf(environ.get("RELEASE_TAG", ""))
    release_model = _bap.strip_crlf(environ.get("RELEASE_MODEL", ""))
    environ["RELEASE_TAG"] = release_tag
    environ["RELEASE_MODEL"] = release_model
    tenants = environ.get("TENANTS", "")
    channel = environ.get("CHANNEL", "")
    if _refuse_tenants_without_ids(tenants):
        return 1

    # Semver-release gate: a release_model=semver app needs an explicit
    # release_tag for any channel='all' publish. Merge-to-main builds the image
    # but only a GitHub Release (which sets release_tag) publishes to all.
    effective = _effective_channel(channel)
    if (
        release_model == "semver"
        and not release_tag
        and not tenant_ids(tenants)
        and effective == "all"
    ):
        print(
            "::error title=Publish blocked::App declares release_model=semver — "
            "publishing to channel='all' requires an explicit release tag. The "
            "image is already in GHCR; cut a GitHub Release from main to publish "
            "to all tenants."
        )
        _bap.summary(
            "### Publish blocked — semver release required",
            "",
            "This app has `release_model: semver` in `atlan.yaml`.",
            "Publishing to the **all** channel requires an explicit GitHub Release.",
            "",
            "| Field | Value |",
            "|-------|-------|",
            f"| Built image | `{environ.get('GHCR_IMAGE', '')}` |",
            f"| Channel | `{effective}` |",
            "",
            "**Next step:** create a GitHub Release from `main` — the image above "
            "is already built and ready.",
        )
        return 1

    try:
        body = json.dumps(build_body(environ))
    except PublishBodyError as exc:
        print(f"::error title=Publish blocked::{exc}")
        return 1
    app_name = environ.get("APP_NAME", "")
    image_tag = environ.get("IMAGE_TAG", "")
    print(f"Publishing {app_name} ({image_tag}) to marketplace...", flush=True)

    proc = _bap.run(
        [
            "curl",
            "-sS",
            "--fail-with-body",
            "-X",
            "POST",
            f"{environ.get('BASE_URL', '')}{PUBLISH_PATH}",
            "-H",
            "Content-Type: application/json",
            # The bearer token is read from stdin (`-H @-`), never placed in
            # argv where any process on the runner could read it.
            "-H",
            "@-",
            "-d",
            body,
        ],
        capture=True,
        input=f"Authorization: Bearer {environ.get('APP_PUBLISH_TOKEN', '')}\n",
    )
    response = (proc.stdout or "").rstrip("\n")
    if proc.returncode != 0:
        print(f"Publish failed: {response}")
        return 1

    data = json.loads(response).get("data", {})
    print(f"Version: {data.get('version_id', 'N/A')}")
    print(f"Release: {data.get('release_id', 'N/A')}")
    print(f"App ID:  {data.get('app_id', 'N/A')}")

    channel_used = channel or "all"
    if tenants:
        channel_used = f"tenants: {tenants}"
    if channel_used == "all":
        print("Channel 'all' creates a draft release — admin approval required")

    _bap.summary(
        "### Publish complete",
        "",
        f"Published `{app_name}` version `{image_tag}` to `{channel_used}`",
        "",
        "Refer to [Global Marketplace](https://globalmarketplaceui.atlan.com/admin/) "
        "for approval.",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("channel-gate", "publish"))
    args = parser.parse_args(argv)
    return channel_gate() if args.command == "channel-gate" else publish()


if __name__ == "__main__":
    _bap.line_buffered_stdout()
    sys.exit(main())
