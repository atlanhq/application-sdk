#!/usr/bin/env python3
"""Build the ``POST /marketplace/publish`` request body.

Why this is its own module
--------------------------
Two call sites register a GM version: the release path
(``build-and-publish-app.yaml``, which builds the body in an inline Python
heredoc) and the e2e path added for FND-31 (``e2e_tenant_app.py``). The whole
point of FND-31 is that the tenant runs *the version under test* — so if the
e2e path registered a differently-shaped version than the release path does,
the e2e would be validating a registration shape that never ships. One builder,
one shape.

The release workflow is deliberately **not** switched over in the same change
that introduces this: it is the path every app release goes through, and
rewiring it to share code with a new e2e flow is the tail wagging the dog.
Instead ``test_marketplace_publish_body.py`` asserts that the field set here
matches the field set that workflow emits, so the two cannot drift unnoticed,
and a follow-up can collapse the duplication safely.

Fields that are conditional rather than always-present are conditional for a
reason — GM distinguishes "absent" from "empty" on several of them (see the
comments at each site), so this builder omits rather than blanks.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

#: Marks a registration as machine-originated. The release workflow sends the
#: same literal; GM uses it for provenance, not for routing.
SOURCE_CI_PUBLISH = "ci_publish"


@dataclass(frozen=True)
class PublishRequest:
    """Everything needed to register one version of one app.

    A frozen dataclass rather than a dict so a caller cannot silently misspell
    a field into oblivion: the publish route ignores unknown keys, so a typo'd
    ``allowed_tenants`` would publish to every tenant instead of failing.

    Attributes:
        app_id: GM app UUID, from the app's ``atlan.yaml``.
        image: Full image reference the tenant will pull.
        version: GM version string. The e2e path uses the image tag, so a
            version is 1:1 with a build.
        branch: Git ref the build came from.
        repo_url: ``https://github.com/<org>/<repo>``. Omitted from the body
            when empty — sending ``""`` makes GM's ``cicd_managed`` validation
            409 for apps registered with a source repo, and carries no signal
            otherwise.
        allowed_tenants: Restrict the version to these tenants. This is what
            keeps per-PR e2e versions from ever reaching a real tenant. When
            empty, ``target_channel`` is sent instead — the two are mutually
            exclusive on the wire.
        target_channel: Channel to publish to when not tenant-targeted.
        deploy_config: Opaque config YAML from ``atlan.yaml``'s ``deploy:``
            block. GM stores it as raw TEXT and echoes it back in the tenant
            catalog.
        self_deployed_runtime: Prepends ``self_deployed_runtime: true`` to the
            config, which is how SDR capability rides to LM without a GM schema
            change.
        sdk_version: application-sdk version resolved from the lockfile.
        entrypoints: ``entrypoints:`` from ``atlan.yaml``. Without it LM's
            catalog merger cannot bind a card to an entrypoint, so a
            multi-entrypoint app serves the wrong form per card.
        app_configs: base64-encoded JSON of the connector form files under
            ``app/generated/``. LM materialises each entry as a ConfigMap.
        release_model: ``semver`` or empty.
        created_by: Actor attribution.
    """

    app_id: str
    image: str
    version: str
    branch: str
    repo_url: str = ""
    allowed_tenants: tuple[str, ...] = ()
    target_channel: str = ""
    deploy_config: str = ""
    self_deployed_runtime: bool = False
    sdk_version: str = ""
    entrypoints: str = ""
    app_configs: str = ""
    release_model: str = ""
    created_by: str = ""


class PublishBodyError(ValueError):
    """The request cannot produce a valid publish body."""


def build(request: PublishRequest) -> dict[str, object]:
    """Return the JSON body for ``POST /api/service/marketplace/publish``."""
    missing = [
        name
        for name in ("app_id", "image", "version", "branch")
        if not str(getattr(request, name) or "").strip()
    ]
    if missing:
        raise PublishBodyError(
            f"cannot build a publish body without: {', '.join(missing)}. "
            "app_id comes from atlan.yaml; image/version/branch come from the "
            "build."
        )

    body: dict[str, object] = {
        "app_id": request.app_id,
        "image": request.image,
        "version": request.version,
        "branch": request.branch,
        "source": SOURCE_CI_PUBLISH,
    }

    # Absence is meaningful — see the attribute docs.
    if request.repo_url:
        body["repo"] = request.repo_url

    config = request.deploy_config.strip()
    if request.self_deployed_runtime:
        config = f"self_deployed_runtime: true\n{config}".strip()
    if config:
        body["config"] = config

    if request.sdk_version.strip():
        body["sdk_version"] = request.sdk_version.strip()

    if request.entrypoints.strip():
        try:
            body["entrypoints"] = json.loads(request.entrypoints)
        except json.JSONDecodeError as exc:
            raise PublishBodyError(
                f"entrypoints is not valid JSON ({exc}). It comes from "
                "parse_atlan_yaml.py, which emits a JSON array."
            ) from exc

    # Mutually exclusive: tenant-targeting takes precedence over the channel,
    # matching the release workflow and the atlan CLI. Sending both would let GM
    # pick, and which one it picks decides whether a per-PR e2e build becomes
    # visible to every tenant — not a coin worth flipping.
    if request.allowed_tenants:
        body["allowed_tenants"] = list(request.allowed_tenants)
    elif request.target_channel.strip():
        body["target_channel"] = request.target_channel.strip()
    else:
        raise PublishBodyError(
            "a publish must be scoped: pass allowed_tenants (what e2e does, so "
            "the version cannot reach a real tenant) or target_channel."
        )

    if request.app_configs.strip():
        body["app_configs"] = request.app_configs.strip()

    if request.release_model.strip():
        body["release_model"] = request.release_model.strip()

    if request.created_by.strip():
        body["created_by"] = request.created_by.strip()

    return body


def main(argv: list[str] | None = None) -> int:
    """Print the body as JSON, for a workflow step that wants to inspect it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repo-url", default="")
    parser.add_argument(
        "--tenant",
        action="append",
        default=[],
        help="Restrict to this tenant. Repeatable. Omit to use --channel.",
    )
    parser.add_argument("--channel", default="")
    parser.add_argument("--deploy-config", default="")
    parser.add_argument("--self-deployed-runtime", action="store_true")
    parser.add_argument("--sdk-version", default="")
    parser.add_argument("--entrypoints", default="")
    parser.add_argument("--app-configs", default="")
    parser.add_argument("--release-model", default="")
    parser.add_argument("--created-by", default="")
    args = parser.parse_args(argv)

    request = PublishRequest(
        app_id=args.app_id,
        image=args.image,
        version=args.version,
        branch=args.branch,
        repo_url=args.repo_url,
        allowed_tenants=tuple(args.tenant),
        target_channel=args.channel,
        deploy_config=args.deploy_config,
        self_deployed_runtime=args.self_deployed_runtime,
        sdk_version=args.sdk_version,
        entrypoints=args.entrypoints,
        app_configs=args.app_configs,
        release_model=args.release_model,
        created_by=args.created_by,
    )
    try:
        print(json.dumps(build(request), indent=2, sort_keys=True))
    except PublishBodyError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
