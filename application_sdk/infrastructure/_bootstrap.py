"""Process-start infrastructure bootstrap: build the :class:`InfrastructureContext`
from the Dapr sidecar.

Moved out of ``application_sdk.main`` (FND-4689). The run modes in ``main`` and
the dev runner in :mod:`application_sdk.dev._runner` call
:func:`_create_infrastructure`; the rest of this module is its startup
diagnostics (which Dapr components registered, and what each expected binding
resolved to) and the ``secretKeyRef`` fetch the object-store resolver needs.

Not re-exported from :mod:`application_sdk.infrastructure`: that package is
imported by every layer, and this module pulls in ``storage`` (lazily) to build
the stores.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from application_sdk.common._env import env_str as _env_str
from application_sdk.errors import DependencyUnavailableError
from application_sdk.main_errors import DaprNotDetectedError
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from application_sdk.infrastructure._dapr.http import AsyncDaprClient
    from application_sdk.infrastructure.context import InfrastructureContext
    from application_sdk.storage.errors import DeploymentObjectStoreMissingError

logger = get_logger(__name__)


# Allowlist of Dapr component metadata keys that are safe to log.
# Keys NOT in this set (e.g. accessKey, secretKey, connectionString, url)
# are silently suppressed even if present in the component YAML.
_SAFE_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "rootPath",
        "bucket",
        "region",
        "endpoint",
        "containerName",
        "accountName",
        "storageAccount",
        "forcePathStyle",
        "brokers",
        "consumerGroup",
        "clientId",
        "authType",
        "actorStateStore",
        "keyPrefix",
        "HotReload",
    }
)


def _parse_all_component_yamls(components_dir: Path) -> dict[str, dict[str, str]]:
    """Parse all Dapr component YAML files and return safe metadata per component.

    Returns a mapping of component name → dict of allowlisted metadata values.
    Non-allowlisted keys (secrets, connection strings, credentials) are never included.
    Silently returns an empty dict on any parse error.

    Values sourced from a ``secretKeyRef`` are redacted to ``"(from secret)"``:
    they resolve to secret-store contents on the secure k8s path, so emitting
    them would leak to startup INFO logs even when the key itself is on the
    allowlist (``endpoint``, ``accountName``).
    """
    import yaml  # noqa: PLC0415 — cold path: yaml only when reading dapr binding YAML

    result: dict[str, dict[str, str]] = {}
    try:
        for yaml_file in sorted(components_dir.glob("*.yaml")):
            with yaml_file.open(encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            if not doc or doc.get("kind") != "Component":
                continue
            name = doc.get("metadata", {}).get("name")
            if not name:
                continue
            spec = doc.get("spec", {})
            safe: dict[str, str] = {}
            for item in spec.get("metadata", []) or []:
                key = item.get("name")
                if not key or key not in _SAFE_METADATA_KEYS:
                    continue
                if "secretKeyRef" in item:
                    safe[key] = "(from secret)"
                elif "value" in item:
                    safe[key] = str(item["value"])
            result[name] = safe
    except Exception:
        logger.warning("Could not parse component YAMLs for diagnostics", exc_info=True)
    return result


async def _log_dapr_components(
    dapr_client: AsyncDaprClient,
    components_dir: Path,
) -> set[str]:
    """Log registered Dapr components and their safe configuration at startup.

    Queries the Dapr sidecar metadata API for all registered components and
    emits one INFO log line per component showing its type, version, and any
    non-sensitive metadata from the corresponding component YAML.

    Then emits one outcome line per *expected* binding (state store, secret
    store, deployment object store, event binding), classifying it against the
    ``ATLAN_ENABLE_*`` env vars the chart derives from ``atlan.yaml``'s
    ``deploy.dapr`` block:

    * registered + declared enabled / unset → INFO ("accepted")
    * registered + declared disabled        → INFO ("drift")
    * missing    + declared enabled         → WARNING ("declared but missing")
    * missing    + declared disabled        → INFO ("disabled by config")
    * missing    + declared unset           → INFO ("legacy/unknown intent")

    The deployment object store is the exception: it is required, so a
    missing one is an ERROR whatever ``ATLAN_ENABLE_OBJECTSTORE`` says, and
    ``_create_infrastructure`` then fails with
    ``DeploymentObjectStoreMissingError``.

    Every binding always produces a log line so operators can see, with a
    reason, how each one was handled. This is best-effort: any failure to
    query Dapr metadata is logged as a WARNING and never blocks startup.

    Returns:
        Set of registered component names. Empty set if metadata query fails.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
        DEPLOYMENT_OBJECT_STORE_NAME,
        EVENT_STORE_NAME,
        SECRET_STORE_NAME,
        STATE_STORE_NAME,
        _read_enable_tri_state,
    )

    try:
        metadata = await dapr_client.get_metadata()
    except Exception:
        logger.warning(
            "Could not query Dapr metadata — component diagnostics unavailable; "
            "optional components (e.g. event binding) will be disabled",
            exc_info=True,
        )
        return set()

    # Dapr 1.13: "registeredComponents", Dapr 1.14+: "components"
    raw_components = metadata.get(
        "components", metadata.get("registeredComponents", [])
    )
    registered = {c["name"]: c for c in raw_components if "name" in c}
    yaml_details = _parse_all_component_yamls(components_dir)

    for name, comp in registered.items():
        safe_meta = yaml_details.get(name, {})
        if safe_meta:
            detail = ", ".join("%s=%s" % (k, v) for k, v in safe_meta.items())
            # conformance: ignore[L006] one-time startup enumeration over discovered dapr components (small, bounded set), not a hot loop; an existing test pins INFO
            logger.info(
                "Dapr component: %s (type=%s, version=%s) — %s",
                name,
                comp.get("type", "unknown"),
                comp.get("version", "unknown"),
                detail,
            )
        else:
            # conformance: ignore[L006] one-time startup enumeration over discovered dapr components (small, bounded set), not a hot loop
            logger.info(
                "Dapr component: %s (type=%s, version=%s)",
                name,
                comp.get("type", "unknown"),
                comp.get("version", "unknown"),
            )

    # Each expected binding is keyed off the ATLAN_ENABLE_* env var the chart
    # derives from atlan.yaml's deploy.dapr block. Tuple shape:
    # (component_name, role_label, enable_env_var, atlan_yaml_key)
    expected: tuple[tuple[str, str, str, str], ...] = (
        (STATE_STORE_NAME, "state_store", "ATLAN_ENABLE_STATESTORE", "statestore"),
        (SECRET_STORE_NAME, "secret_store", "ATLAN_ENABLE_SECRETSTORE", "secretstore"),
        (
            DEPLOYMENT_OBJECT_STORE_NAME,
            "object_store",
            "ATLAN_ENABLE_OBJECTSTORE",
            "objectstore",
        ),
        (EVENT_STORE_NAME, "event_binding", "ATLAN_ENABLE_EVENTSTORE", "eventstore"),
    )
    for comp_name, role, enable_var, yaml_key in expected:
        raw, declared = _read_enable_tri_state(enable_var)
        is_registered = comp_name in registered

        if comp_name == DEPLOYMENT_OBJECT_STORE_NAME and not is_registered:
            # Required whatever the flag says. The chart sets ATLAN_ENABLE_* only
            # on the env-seeder init container, so the app container usually
            # sees it unset; an absent object store is never "expected".
            logger.error(
                "Dapr binding %s (role=%s) is not registered in sidecar (%s=%s) — "
                "the deployment object store is required and startup will fail; "
                "set deploy.dapr.objectstore to true in the app contract",
                comp_name,
                role,
                enable_var,
                raw if raw is not None else "<unset>",
            )
        elif is_registered and declared is not False:
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) accepted: registered in sidecar (%s=%s)",
                comp_name,
                role,
                enable_var,
                raw if raw is not None else "<unset>",
            )
        elif is_registered and declared is False:
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) registered in sidecar despite %s=false — "
                "possible drift between atlan.yaml deploy.dapr.%s and the deployed chart values",
                comp_name,
                role,
                enable_var,
                yaml_key,
            )
        elif not is_registered and declared is True:
            logger.warning(
                "Dapr binding %s (role=%s) declared enabled (%s=true) but not registered in sidecar — "
                "runtime calls using this component will fail; check chart values and Dapr component templates",
                comp_name,
                role,
                enable_var,
            )
        elif not is_registered and declared is False:
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) disabled by config (%s=false); not registered in sidecar (expected)",
                comp_name,
                role,
                enable_var,
            )
        else:  # not registered, declared is None
            # conformance: ignore[L006] one-time startup validation over the fixed 4-element `expected` tuple, not a hot loop
            logger.info(
                "Dapr binding %s (role=%s) not registered in sidecar and %s is unset — "
                "the SDK cannot determine whether your app needs it; "
                "set deploy.dapr.%s in atlan.yaml (true or false) to silence this",
                comp_name,
                role,
                enable_var,
                yaml_key,
            )

    return set(registered)


async def _fetch_binding_secrets(
    dapr_client: AsyncDaprClient,
    name: str,
    *,
    components_dir: Path | str,
    required: bool = False,
) -> dict[str, dict[str, str]]:
    """Fetch the secrets a Dapr component's ``secretKeyRef`` entries point at.

    The obstore resolver is synchronous and public, so it cannot call the Dapr
    secret API itself.  This runs after ``wait_for_dapr_sidecar()``, where the
    sidecar is known ready, and hands the result to the resolver as ``secrets=``.

    Returns ``{}`` when the component declares no ``auth.secretStore`` — that
    is the Docker Compose / ``secretstores.local.env`` shape, which the
    resolver already covers through environment variables.

    When ``required`` is true (e.g. the upstream store under
    ``ENABLE_ATLAN_UPLOAD=true``), a secret that cannot be read is re-raised
    with ``from exc`` so a secret-store outage surfaces as the real failure
    instead of being misreported downstream as ``StorageBindingBrokenError``.
    When ``required`` is false the failure is logged and skipped: the resolver
    reports the resulting broken fields by name, which says far more than a
    lookup failure would.
    """
    from application_sdk.storage.binding import (  # noqa: PLC0415 — cold path: startup wiring only
        read_binding_secret_refs,
    )

    declared = read_binding_secret_refs(name, components_dir=components_dir)
    if declared.secret_store is None:
        return {}

    secrets: dict[str, dict[str, str]] = {}
    for secret_name in declared.secret_names:
        try:
            secrets[secret_name] = await dapr_client.get_secret(
                declared.secret_store, secret_name
            )
        # conformance: ignore[E004] one unreadable secret must not mask the rest; the resolver names the broken fields
        except Exception as exc:
            if required:
                raise DependencyUnavailableError(
                    message=(
                        f"Could not read secret '{secret_name}' from secret store "
                        f"'{declared.secret_store}' for Dapr component '{name}' — "
                        f"the binding is required, so the secret-store failure is "
                        f"fatal"
                    ),
                    service="dapr-secret-store",
                    target=declared.secret_store,
                    # Not the leaf's retryable=True default. This `except`
                    # catches a transient store outage and a secret that is
                    # missing or misnamed alike, and cannot tell them apart —
                    # only the first is fixed by retrying. Claiming retryable
                    # on the pair invites a retry loop on a misconfiguration,
                    # where the pre-typed behaviour was to fail outright.
                    retryable=False,
                    cause=exc,
                ) from exc
            logger.warning(
                "Could not read secret '%s' from secret store '%s' for Dapr "
                "component '%s' — the binding may resolve as broken",
                secret_name,
                declared.secret_store,
                name,
                exc_info=True,
            )
    return secrets


def _deployment_object_store_missing(
    name: str, components_dir: Path, cause: Exception
) -> DeploymentObjectStoreMissingError:
    """Log and build the startup error for an absent deployment object store.

    Keyed on the missing component, never on ``ATLAN_ENABLE_OBJECTSTORE``: the
    chart sets that flag only on the env-seeder init container, so the app
    container usually sees it unset.  When it is visible and false, the
    message says so, because then the contract itself disabled the store.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
        _read_enable_tri_state,
    )
    from application_sdk.storage.errors import (  # noqa: PLC0415 — cold path: startup failure only
        DeploymentObjectStoreMissingError,
    )

    _, declared = _read_enable_tri_state("ATLAN_ENABLE_OBJECTSTORE")
    flag_note = (
        " ATLAN_ENABLE_OBJECTSTORE=false is set in this container: the app's "
        "contract disables the object store."
        if declared is False
        else ""
    )
    message = (
        f"No Dapr component named '{name}' found in {components_dir}. The "
        "deployment object store is required; the atlan-app chart omits it "
        "when the app's contract sets deploy.dapr.objectstore to false. Set "
        "deploy.dapr.objectstore to true (dapr {{ objectstore = true }} in "
        f"contract/app.pkl), regenerate atlan.yaml and redeploy.{flag_note}"
    )
    logger.error("Deployment object store missing at startup: %s", message)
    return DeploymentObjectStoreMissingError(message, binding_name=name, cause=cause)


async def _create_infrastructure() -> InfrastructureContext:
    """Create infrastructure services based on environment.

    If ``DAPR_HTTP_PORT`` is set (Dapr sidecar present), creates Dapr-backed
    implementations. Otherwise raises ``RuntimeError`` — the Dapr sidecar is
    required for all runtime modes.

    Returns:
        Configured InfrastructureContext.

    Raises:
        DaprNotDetectedError: If DAPR_HTTP_PORT is not set (no Dapr sidecar).
        DeploymentObjectStoreMissingError: If the deployment object-store
            component is absent — the store is required (set
            ``deploy.dapr.objectstore`` to true in the app contract).
    """
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
        InfrastructureContext,
    )

    if os.environ.get("DAPR_HTTP_PORT"):
        from pathlib import (  # noqa: PLC0415 — cold path: lazy load for entry-point function
            Path,
        )

        from application_sdk.constants import (  # noqa: PLC0415 — cold path: lazy access to env-var-derived constants
            DEPLOYMENT_OBJECT_STORE_NAME,
            ENABLE_ATLAN_UPLOAD,
            EVENT_STORE_NAME,
            SECRET_STORE_NAME,
            STATE_STORE_NAME,
            UPSTREAM_OBJECT_STORE_NAME,
        )
        from application_sdk.infrastructure._dapr.client import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
            DaprBinding,
            DaprSecretStore,
            DaprStateStore,
        )
        from application_sdk.infrastructure._dapr.http import (  # noqa: PLC0415 — cold path: only when infrastructure init is needed
            AsyncDaprClient,
            wait_for_dapr_sidecar,
        )
        from application_sdk.storage import (  # noqa: PLC0415 — cold path: storage init only when binding YAML present
            create_store_from_binding_with_put_attrs,
        )
        from application_sdk.storage.binding import (  # noqa: PLC0415 — cold path: private helper used only inside _create_infrastructure
            _create_store_from_binding_optional_with_put_attrs,
        )
        from application_sdk.storage.errors import (  # noqa: PLC0415 — cold path: storage init only when Dapr is present
            StorageBindingNotFoundError,
        )

        await wait_for_dapr_sidecar()
        dapr_client = AsyncDaprClient()
        components_dir = Path(_env_str("DAPR_COMPONENTS_PATH", "./components"))
        registered_components = await _log_dapr_components(dapr_client, components_dir)
        logger.info("Dapr sidecar detected — using Dapr infrastructure")

        # BLDX-1619: on the k8s SDR secure path a store component resolves its
        # credentials through auth.secretStore, and the matching env vars are
        # deliberately absent. Fetch those secrets here — the sidecar is up — so
        # the resolver sees the same values the sidecar does.  Publish them to
        # the binding-layer cache so later sync consumers (notably
        # ``DaprCredentialVault.__init__``) reach the same upstream-vs-deployment
        # verdict instead of re-deriving a different one env-only.
        from application_sdk.storage.binding import (  # noqa: PLC0415 — cold path: startup wiring only
            set_fetched_binding_secrets,
        )

        deployment_secrets = await _fetch_binding_secrets(
            dapr_client,
            DEPLOYMENT_OBJECT_STORE_NAME,
            components_dir=components_dir,
        )
        set_fetched_binding_secrets(DEPLOYMENT_OBJECT_STORE_NAME, deployment_secrets)
        try:
            deployment_store, deployment_put_attrs = (
                create_store_from_binding_with_put_attrs(
                    DEPLOYMENT_OBJECT_STORE_NAME,
                    components_dir=components_dir,
                    secrets=deployment_secrets,
                )
            )
        except StorageBindingNotFoundError as exc:
            raise _deployment_object_store_missing(
                DEPLOYMENT_OBJECT_STORE_NAME, components_dir, exc
            ) from exc

        # The only place the two component *names* are compared: identity cannot
        # be tested before the second object exists, so this is the decision
        # about what to build.  Everywhere downstream asks ``single_store``,
        # which compares the handles this branch produced.
        if UPSTREAM_OBJECT_STORE_NAME == DEPLOYMENT_OBJECT_STORE_NAME:
            # In-cluster charts point both names at the app's one object-store
            # component.  One component is one store, so alias the handle rather
            # than build a second store object over the same bucket: identity is
            # what the dual-write fan-out (``upstream is not deployment``) and
            # ``_upload_from_store``'s same-object guard test, and two objects
            # make ``App.upload`` copy the bucket onto itself — a per-key sidecar
            # compare inside the framework task's fixed timeout.
            #
            # Aliasing, not ``None``: every consumer outside ``App.upload`` reads
            # ``upstream_storage`` as "is there an upstream store", and a name
            # gate that answers ``None`` there is the fragile heuristic 668a06c21
            # removed.  ``context.single_store`` is the topology signal.
            if ENABLE_ATLAN_UPLOAD:
                logger.warning(
                    "ENABLE_ATLAN_UPLOAD is set but UPSTREAM_OBJECT_STORE_NAME and "
                    "DEPLOYMENT_OBJECT_STORE_NAME both name %r — one object store. "
                    "App.upload hands artifacts to that store; nothing crosses to a "
                    "separate Atlan bucket. Point UPSTREAM_OBJECT_STORE_NAME at the "
                    "Atlan object-store component if this deployment is meant to "
                    "hand off across the boundary.",
                    DEPLOYMENT_OBJECT_STORE_NAME,
                )
            else:
                logger.info(
                    "UPSTREAM_OBJECT_STORE_NAME and DEPLOYMENT_OBJECT_STORE_NAME both "
                    "name %r — one object store; upstream_storage aliases it",
                    DEPLOYMENT_OBJECT_STORE_NAME,
                )
            upstream_storage, upstream_put_attrs = (
                deployment_store,
                deployment_put_attrs,
            )
        else:
            # BLDX-1619: the upstream component resolves its credentials through
            # auth.secretStore on the k8s SDR secure path, so fetch those secrets
            # too before building the store.
            upstream_secrets = await _fetch_binding_secrets(
                dapr_client,
                UPSTREAM_OBJECT_STORE_NAME,
                components_dir=components_dir,
                required=ENABLE_ATLAN_UPLOAD,
            )
            set_fetched_binding_secrets(UPSTREAM_OBJECT_STORE_NAME, upstream_secrets)
            upstream_storage, upstream_put_attrs = (
                _create_store_from_binding_optional_with_put_attrs(
                    UPSTREAM_OBJECT_STORE_NAME,
                    components_dir=components_dir,
                    required=ENABLE_ATLAN_UPLOAD,
                    secrets=upstream_secrets,
                )
            )
        return InfrastructureContext(
            state_store=DaprStateStore(dapr_client, store_name=STATE_STORE_NAME),
            secret_store=DaprSecretStore(dapr_client, store_name=SECRET_STORE_NAME),
            storage=deployment_store,
            storage_put_attributes=deployment_put_attrs,
            upstream_storage=upstream_storage,
            upstream_storage_put_attributes=upstream_put_attrs,
            event_binding=(
                DaprBinding(dapr_client, EVENT_STORE_NAME)
                if EVENT_STORE_NAME in registered_components
                else None
            ),
            _dapr_client=dapr_client,
        )
    else:
        raise DaprNotDetectedError()
