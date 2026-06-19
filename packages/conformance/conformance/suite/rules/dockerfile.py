"""Container image conformance rule definitions (I-series).

Every consumer app ships a Dockerfile used by the platform to build and run
the application container.  These rules enforce the structural constraints that
keep the fleet predictable: the correct base image, the base-image entrypoint
contract, the required module-discovery env var, and non-root execution.

* ``I001`` — FROM must be ``registry.atlan.com/public/app-runtime-base:3``.
  Reject ``*-latest``, dev-branch tags, raw ``cgr.dev/.../python``, or any
  other base image.  The v3 major tag is the only accepted form.

* ``I002`` — CMD and ENTRYPOINT must not be overridden.  The base image
  co-launches ``daprd`` and handles graceful shutdown on SIGTERM; overriding
  either instruction silently bypasses both.  Classic fail-green: the app boots
  fine locally, then loses graceful drain in prod.

* ``I003`` — ``ENV ATLAN_APP_MODULE=<module>:<AppClass>`` must be set.  The
  runtime uses this variable to locate and instantiate the application class.
  An image without it will fail to start.

* ``I004`` — ``ENV ATLAN_APP_MODE`` must not be hardcoded in the Dockerfile.
  Runtime mode (``worker`` / ``server``) must be supplied at deploy time via
  the deployment environment, not baked into the image — different deployments
  of the same image may require different modes.

* ``I005`` — No ``USER`` instruction in the final stage may set the user to
  ``root`` (``root`` or ``0``).  The base image already runs as ``appuser``;
  ``USER root`` silently reverses that and exposes the container to
  privilege-escalation risks.
"""

from __future__ import annotations

from conformance.suite.schema.catalog import RuleDefinition
from conformance.suite.schema.disposition import (
    EnforcementTier,
    RuleMechanism,
    RuleScope,
)

RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition(
        id="I001",
        scope=RuleScope.APP,
        name="DockerfileWrongBaseImage",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dockerfile-base",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "Only registry.atlan.com/public/app-runtime-base:3 carries the "
            "daprd sidecar, the entrypoint script, the appuser setup, and the "
            "standard env vars the platform expects.  Any other base — raw "
            "upstream Python, cgr.dev images, or a stale/dev-branch tag of "
            "app-runtime-base — silently omits one or more of these, producing "
            "a container that passes local CI but fails in prod (missing graceful "
            "drain, missing Dapr sidecar, wrong user, or broken env)."
        ),
        short_description=(
            "Final-stage FROM does not use the approved base image "
            "registry.atlan.com/public/app-runtime-base:3"
        ),
        full_description=(
            "The final-stage ``FROM`` instruction must be exactly "
            "``registry.atlan.com/public/app-runtime-base:3``.  The v3 major "
            "tag is the only accepted form: ``*-latest``, dev-branch tags "
            "(e.g. ``:main``), pinned patch versions (e.g. ``:3.2.1``), "
            "raw upstream Python images, and any other registry or image name "
            "are rejected.  Intermediate builder-stage FROMs in multi-stage "
            "builds are not checked — only the last ``FROM`` in the file "
            "determines the runtime base.  Inline suppression: "
            "``# conformance: ignore[I001] <reason>`` on the line before "
            "the FROM instruction."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dockerfile.md#i001"
        ),
    ),
    RuleDefinition(
        id="I002",
        scope=RuleScope.APP,
        name="DockerfileEntrypointOverride",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dockerfile-entrypoint",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "The base image entrypoint script co-launches the Dapr sidecar "
            "(daprd) alongside the application and handles graceful drain on "
            "SIGTERM — forwarding the signal to both processes and waiting for "
            "clean shutdown.  Overriding CMD or ENTRYPOINT silently bypasses "
            "both: the app boots fine locally (no daprd needed there) but loses "
            "graceful drain in prod, causing in-flight requests to be dropped "
            "during rolling restarts or scale-down events."
        ),
        short_description=(
            "CMD or ENTRYPOINT is overridden; the base image entrypoint "
            "manages daprd and graceful drain and must not be replaced"
        ),
        full_description=(
            "Neither ``CMD`` nor ``ENTRYPOINT`` may appear in the app "
            "Dockerfile.  The base image (``app-runtime-base``) ships an "
            "entrypoint script that co-launches ``daprd`` alongside the "
            "application process and forwards SIGTERM to both, ensuring "
            "graceful drain during rolling restarts and scale-down.  Overriding "
            "either instruction replaces this script without warning, "
            "disconnecting the Dapr sidecar and breaking graceful shutdown in "
            "any environment where daprd is required.  Inline suppression: "
            "``# conformance: ignore[I002] <reason>`` on the line before "
            "the offending instruction."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dockerfile.md#i002"
        ),
    ),
    RuleDefinition(
        id="I003",
        scope=RuleScope.APP,
        name="DockerfileAppModuleMissing",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dockerfile-env",
        autofixable=False,
        since="0.5.0",
        rationale=(
            "The platform runtime discovers the application class via "
            "ATLAN_APP_MODULE at container start.  An image that omits this "
            "variable will fail to start with a cryptic import error — not a "
            "build error — making the failure invisible until the container is "
            "actually deployed.  Enforcing the variable at lint time closes "
            "that gap."
        ),
        short_description=(
            "ENV ATLAN_APP_MODULE is not set; the runtime needs this to "
            "locate and instantiate the application class"
        ),
        full_description=(
            "The Dockerfile must contain ``ENV ATLAN_APP_MODULE=<module>:<AppClass>`` "
            "with a non-empty value.  The platform runtime imports this module "
            "path and instantiates the named class to start the application.  "
            "Both the key=value form (``ENV ATLAN_APP_MODULE=myapp.app:MyApp``) "
            "and the deprecated space-separated form "
            "(``ENV ATLAN_APP_MODULE myapp.app:MyApp``) are recognised.  "
            "To suppress this finding for an unusual build pattern, add "
            "``# conformance: ignore[I003] <reason>`` to the first line of "
            "the Dockerfile."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dockerfile.md#i003"
        ),
    ),
    RuleDefinition(
        id="I004",
        scope=RuleScope.APP,
        name="DockerfileAppModeHardcoded",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dockerfile-env",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "The same image may be deployed as a worker or a server depending "
            "on the deployment target.  Hardcoding ATLAN_APP_MODE in the "
            "Dockerfile bakes the mode decision into the image, preventing "
            "multi-mode deployments and making it easy to accidentally ship "
            "the wrong mode to prod.  The value must be injected at deploy "
            "time via the deployment manifest."
        ),
        short_description=(
            "ENV ATLAN_APP_MODE is hardcoded in the Dockerfile; runtime mode "
            "must be supplied at deploy time, not baked into the image"
        ),
        full_description=(
            "``ENV ATLAN_APP_MODE`` must not appear in the Dockerfile.  "
            "Runtime mode (``worker`` / ``server``) is deployment-specific: "
            "the same image may be deployed in different modes in different "
            "environments.  Set ``ATLAN_APP_MODE`` in the deployment manifest "
            "(Kubernetes ``env``, Helm values, or equivalent) instead of "
            "hardcoding it here.  Inline suppression: "
            "``# conformance: ignore[I004] <reason>`` on the line before "
            "the offending ENV instruction."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dockerfile.md#i004"
        ),
    ),
    RuleDefinition(
        id="I005",
        scope=RuleScope.APP,
        name="DockerfileRootUser",
        tier=EnforcementTier.BLOCK,
        mechanism=RuleMechanism.STATIC,
        category="dockerfile-security",
        autofixable=True,
        since="0.5.0",
        rationale=(
            "The base image already establishes appuser as the container user.  "
            "A USER root (or USER 0) instruction in the final stage reverses "
            "this, running the application process as root and exposing the "
            "container to privilege-escalation risks.  This is the exact pattern "
            "that turns a container escape into a host root compromise."
        ),
        short_description=(
            "USER root or USER 0 in the final stage sets the container user "
            "to root, violating the non-root execution policy"
        ),
        full_description=(
            "No ``USER root`` or ``USER 0`` instruction may appear in the final "
            "stage of the Dockerfile (after the last ``FROM``).  The base image "
            "(``app-runtime-base``) already runs as ``appuser``; any instruction "
            "that changes to root overrides this silently and violates the "
            "non-root container execution policy.  ``USER root`` in intermediate "
            "builder stages of a multi-stage build is not flagged — only the "
            "final stage's user matters for the runtime container.  Inline "
            "suppression: ``# conformance: ignore[I005] <reason>`` on the line "
            "before the offending USER instruction."
        ),
        help_uri=(
            "https://github.com/atlanhq/application-sdk/blob/main/"
            "packages/conformance/conformance/docs/rules/dockerfile.md#i005"
        ),
    ),
)
