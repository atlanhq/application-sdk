"""SDR boot-time object-store access preflight.

When an app runs in Self-Deployed Runtime (SDR) mode (``ENABLE_ATLAN_UPLOAD=true``),
the entire flow depends on read+write access to one or two object stores.  If access
is broken — missing binding, invalid credentials, or insufficient permissions — every
run will fail deep inside a workflow.  This module surfaces those failures immediately
at process boot, before the Temporal worker starts serving.

Public entry point::

    from application_sdk.storage.preflight import verify_object_store_access
    await verify_object_store_access(infra)

This function is a no-op when SDR mode is not active.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.infrastructure.context import InfrastructureContext

logger = get_logger(__name__)

_PREFLIGHT_PREFIX = ".atlan-preflight"
_PREFLIGHT_PAYLOAD = b"atlan-preflight"


def _classify_access_error(exc: BaseException) -> tuple[str, str]:
    """Classify an obstore exception into (error_class, remediation_hint).

    Mirrors the substring-matching pattern used by ``_is_not_found`` and
    ``_is_azure_container_not_found`` in ``ops.py`` — class-based matching is
    preferred when available; substring fallback covers generic obstore errors.

    Returns:
        A 2-tuple of (error_class_label, remediation_hint).
    """
    msg = str(exc).lower()

    if any(
        marker in msg
        for marker in (
            "403",
            "accessdenied",
            "access denied",
            "forbidden",
            "not authorized",
            "authorization failed",
        )
    ):
        return (
            "permission denied",
            "The credentials are valid but lack the required read/write/delete "
            "permissions on this bucket. Grant the IAM/ACL permissions needed "
            "for get, put, and delete operations.",
        )

    if any(
        marker in msg
        for marker in (
            "401",
            "invalidaccesskeyid",
            "invalid access key",
            "signaturedoesnotmatch",
            "unauthenticated",
            "invalid credentials",
        )
    ):
        return (
            "invalid credentials",
            "The credentials in the Dapr component appear to be invalid "
            "(wrong key, expired token, or malformed secret). Update the binding "
            "component or the referenced secret values.",
        )

    return (
        "connectivity / unknown",
        "Could not reach the object store. Check the endpoint URL, "
        "bucket/container name, and network connectivity from this pod/host.",
    )


async def _probe_store(store: object, label: str, binding_name: str) -> str | None:
    """Run a write → read → delete round-trip against *store*.

    Args:
        store: An obstore-compatible store instance.
        label: Human-readable role label ("deployment" or "upstream").
        binding_name: The Dapr component name (used in error messages).

    Returns:
        ``None`` on success; a human-readable failure description on error.
    """
    import obstore  # noqa: PLC0415 — lazy: obstore is a heavy Rust extension; defer until actually needed

    probe_key = f"{_PREFLIGHT_PREFIX}/{uuid.uuid4()}.probe"
    logger.info(
        "SDR preflight: probing %s store (%s) — key=%s",
        label,
        binding_name,
        probe_key,
    )

    # Write
    try:
        await obstore.put_async(store, probe_key, _PREFLIGHT_PAYLOAD)
    except Exception as exc:
        error_class, hint = _classify_access_error(exc)
        return (
            f"  * {label} store (binding: '{binding_name}'): write failed [{error_class}]\n"
            f"    Cause: {exc}\n"
            f"    Hint:  {hint}"
        )

    # Read (HEAD) — confirms read permission and that the write was committed
    read_failure: str | None = None
    try:
        await obstore.head_async(store, probe_key)
    except Exception as exc:
        error_class, hint = _classify_access_error(exc)
        read_failure = (
            f"  * {label} store (binding: '{binding_name}'): read/head failed [{error_class}]\n"
            f"    Cause: {exc}\n"
            f"    Hint:  {hint}"
        )

    # Delete — best-effort cleanup; a leaked probe object is not fatal
    try:
        await obstore.delete_async(store, probe_key)
    except Exception as exc:
        logger.warning(
            "SDR preflight: could not delete probe object from %s store "
            "(binding='%s', key=%s): %s — object may be left behind",
            label,
            binding_name,
            probe_key,
            exc,
        )

    return read_failure


async def verify_object_store_access(infra: InfrastructureContext) -> None:
    """In SDR mode, verify read+write access to every configured object store.

    Performs a write → read → delete round-trip on the deployment store and,
    when configured, the upstream Atlan store.  Also hard-fails if SDR mode is
    active but the upstream store is absent (which would cause artifacts to
    silently never reach Atlan).

    This function is a **no-op when** ``ENABLE_ATLAN_UPLOAD`` **is falsy**
    — it is only meaningful in Self-Deployed Runtime deployments.

    Args:
        infra: The fully populated ``InfrastructureContext`` returned by
            ``_create_infrastructure`` in ``main.py``.

    Raises:
        ObjectStorePreflightError: If any store is inaccessible or the upstream
            store is absent while SDR mode is enabled.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: SDR-gated; constants only loaded when needed
        DEPLOYMENT_OBJECT_STORE_NAME,
        ENABLE_ATLAN_UPLOAD,
        UPSTREAM_OBJECT_STORE_NAME,
    )
    from application_sdk.storage.errors import (  # noqa: PLC0415 — cold path: avoids a module-level circular import
        ObjectStorePreflightError,
    )

    if not ENABLE_ATLAN_UPLOAD:
        return

    logger.info(
        "SDR mode active (ENABLE_ATLAN_UPLOAD=true) — running object-store access preflight"
    )

    failures: list[str] = []

    # Hard-fail if upstream store absent: in SDR mode every artifact must be
    # uploaded to the Atlan bucket; a missing upstream store means silent data loss.
    if infra.upstream_storage is None:
        failures.append(
            f"  * upstream store (binding: '{UPSTREAM_OBJECT_STORE_NAME}'): not configured\n"
            "    SDR mode is enabled (ENABLE_ATLAN_UPLOAD=true) but the upstream Atlan\n"
            "    object store is absent — artifacts produced by this connector would\n"
            "    never reach Atlan.\n"
            f"    Hint:  Add a Dapr component named '{UPSTREAM_OBJECT_STORE_NAME}' to\n"
            "           the components directory and ensure its credentials are resolvable."
        )

    # Round-trip probe each store
    stores_to_probe: list[tuple[str, str, object]] = [
        ("deployment", DEPLOYMENT_OBJECT_STORE_NAME, infra.storage),
    ]
    if infra.upstream_storage is not None:
        stores_to_probe.append(
            ("upstream", UPSTREAM_OBJECT_STORE_NAME, infra.upstream_storage)
        )

    for label, binding_name, store in stores_to_probe:
        if store is None:
            failures.append(
                f"  * {label} store (binding: '{binding_name}'): store is None — "
                "check that the binding component is present and parseable"
            )
            continue
        failure = await _probe_store(store, label, binding_name)
        if failure is not None:
            failures.append(failure)

    if failures:
        count = len(failures)
        summary = "\n".join(failures)
        raise ObjectStorePreflightError(
            f"Object-store access check failed ({count} store(s) with errors):\n{summary}",
            failure_count=count,
        )

    logger.info("SDR preflight: all object-store access checks passed")
