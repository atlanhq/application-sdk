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

Timeout:
    Each per-store probe is bounded by ``ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS``
    (default: 30 s).  A blackholed endpoint that would otherwise stall the
    boot indefinitely is classified as ``connectivity / unknown``.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from application_sdk.infrastructure.context import InfrastructureContext

logger = get_logger(__name__)

_PREFLIGHT_PREFIX = "artifacts/apps/.atlan-sdk-preflight"
_PREFLIGHT_PAYLOAD = b"atlan-preflight"

# Per-store probe timeout.  Keeps a blackholed endpoint from stalling boot
# indefinitely — obstore wraps the Rust object_store crate, whose default
# connect_timeout and request timeout are both None.
_PROBE_TIMEOUT_SECS: float = 30.0
_raw_timeout = os.environ.get("ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS")
if _raw_timeout is not None:
    try:
        _parsed_timeout = float(_raw_timeout)
        if _parsed_timeout > 0:
            _PROBE_TIMEOUT_SECS = _parsed_timeout
        else:
            logger.warning(
                "ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS=%r is non-positive; using default %.0f s",
                _raw_timeout,
                _PROBE_TIMEOUT_SECS,
            )
    except ValueError:
        logger.warning(
            "ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS=%r is not a valid number; using default %.0f s",
            _raw_timeout,
            _PROBE_TIMEOUT_SECS,
            exc_info=True,
        )
del _raw_timeout

# Stable probe key reused across boots on the same host so that a principal
# with put+head but no delete permission doesn't accumulate one orphaned
# object per boot.  Must stay under ``artifacts/apps/`` (or another allowed
# prefix) — the Kong s3proxy plugin enforces an upstream_path_prefixes
# allowlist and rejects anything outside it with 403 code 1009.
_PROBE_KEY = f"{_PREFLIGHT_PREFIX}/probe-{socket.gethostname()}"

# Pre-compiled patterns for HTTP status codes.  Word-boundary anchors prevent
# false positives from request IDs, byte counts, or longer strings that happen
# to contain "401" / "403" as a substring.
_RE_403 = re.compile(r"\b403\b")
_RE_401 = re.compile(r"\b401\b")

# Connectivity/unknown hint — shared between the classifier's fallback bucket
# and the timeout branch so a future classifier change cannot silently diverge.
_CONNECTIVITY_HINT = (
    "Could not reach the object store. Check the endpoint URL, "
    "bucket/container name, and network connectivity from this pod/host."
)


def _classify_access_error(exc: BaseException) -> tuple[str, str]:
    """Classify an obstore exception into (error_class, remediation_hint).

    Uses obstore's structured exception classes when available; falls back to
    pattern matching on the lowercased error message.  HTTP status codes use
    word-boundary regex (``\\b403\\b``) to avoid false positives from request
    IDs or byte counts that happen to contain those digits.

    Returns:
        A 2-tuple of (error_class_label, remediation_hint).
    """
    msg = str(exc).lower()

    if _RE_403.search(msg) or any(
        marker in msg
        for marker in (
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

    if _RE_401.search(msg) or any(
        marker in msg
        for marker in (
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
        _CONNECTIVITY_HINT,
    )


async def _probe_store(store: object, label: str, binding_name: str) -> str | None:
    """Run a write → read → delete round-trip against *store*.

    Each operation is unbounded in this function; the caller wraps the whole
    coroutine in ``asyncio.wait_for`` to enforce the boot-time timeout.

    Delete is best-effort: a delete failure logs a warning but does not raise.

    Args:
        store: An obstore-compatible store instance.
        label: Human-readable role label ("deployment" or "upstream").
        binding_name: The Dapr component name (used in error messages).

    Returns:
        ``None`` on success; a human-readable failure description on error.
    """
    import obstore  # noqa: PLC0415 — lazy: obstore is a heavy Rust extension; defer until actually needed

    probe_key = _PROBE_KEY
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

    # Delete — best-effort cleanup; uses the stable per-host key so a missing
    # delete permission doesn't accumulate one new object per boot.
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
            exc_info=True,
        )

    return read_failure


async def verify_object_store_access(infra: InfrastructureContext) -> None:
    """In SDR mode, verify read+write access to every configured object store.

    Performs a write → read → delete round-trip on the deployment store and,
    when configured, the upstream Atlan store.  Also hard-fails if SDR mode is
    active but the upstream store is absent — this is a defense-in-depth check;
    the primary guard is ``_create_store_from_binding_optional_with_put_attrs``
    raising ``StorageBindingNotFoundError`` at construction time.

    Each per-store probe is bounded by ``ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS``
    (default: 30 s).  A probe that times out is classified as
    ``connectivity / unknown``.

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

    # Defense-in-depth: hard-fail if upstream store absent in SDR mode.
    # The primary guard is the binding factory raising StorageBindingNotFoundError
    # at construction time (required=ENABLE_ATLAN_UPLOAD); this check catches any
    # caller that bypasses the factory and passes upstream_storage=None directly.
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
        try:
            failure = await asyncio.wait_for(
                _probe_store(store, label, binding_name),
                timeout=_PROBE_TIMEOUT_SECS,
            )
        except TimeoutError:
            failure = (
                f"  * {label} store (binding: '{binding_name}'): probe timed out "
                f"after {_PROBE_TIMEOUT_SECS:.0f}s [connectivity / unknown]\n"
                f"    Hint:  {_CONNECTIVITY_HINT}\n"
                f"    Tip:   Override timeout via ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS."
            )
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
