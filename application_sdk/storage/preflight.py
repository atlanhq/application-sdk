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
from dataclasses import dataclass
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

# Fixed probe key overwritten on every boot — guarantees a single object per
# store with no accumulation.  A hostname-scoped key would create one object
# per unique pod name in k8s and never converge.  Since we always write before
# reading, a stale probe from a previous run cannot produce a false positive.
# Must stay under ``artifacts/apps/`` (or another allowed prefix) — the Kong
# s3proxy plugin enforces an upstream_path_prefixes allowlist and rejects
# anything outside it with 403 code 1009.
_PROBE_KEY = f"{_PREFLIGHT_PREFIX}/probe"

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

# Bucket for a binding that was never probed because it is absent or
# unparseable.  Bypasses the classifier entirely (there is no exception to
# classify), so it is named here rather than derived from a rule.
_NOT_CONFIGURED = "not configured"
_CONNECTIVITY_UNKNOWN = "connectivity / unknown"


@dataclass(frozen=True)
class _AccessErrorRule:
    """One classifier bucket: how to recognise it, and what to tell the reader.

    Table-driven rather than a chain of ``if`` branches so the set of buckets
    the probe can emit is *enumerable* — see :data:`_OBJECT_STORE_ERROR_CLASSES`.
    A new bucket added as a branch could fall through the consumer's
    bucket-to-error-leaf mapping unnoticed; a new bucket added as a rule cannot.
    """

    bucket: str
    hint: str
    pattern: re.Pattern[str] | None = None
    markers: tuple[str, ...] = ()

    def matches(self, msg: str) -> bool:
        """Whether *msg* (already lowercased) falls in this bucket."""
        if self.pattern is not None and self.pattern.search(msg):
            return True
        return any(marker in msg for marker in self.markers)


_ACCESS_ERROR_RULES: tuple[_AccessErrorRule, ...] = (
    _AccessErrorRule(
        bucket="permission denied",
        pattern=_RE_403,
        markers=(
            "accessdenied",
            "access denied",
            "forbidden",
            "not authorized",
            "authorization failed",
        ),
        hint=(
            "The credentials are valid but lack the required read/write "
            "permissions on this bucket. Grant the IAM/ACL permissions needed "
            "for get and put operations."
        ),
    ),
    _AccessErrorRule(
        bucket="invalid credentials",
        pattern=_RE_401,
        markers=(
            "invalidaccesskeyid",
            "invalid access key",
            "signaturedoesnotmatch",
            "unauthenticated",
            "invalid credentials",
        ),
        hint=(
            "The credentials in the Dapr component appear to be invalid "
            "(wrong key, expired token, or malformed secret). Update the binding "
            "component or the referenced secret values."
        ),
    ),
)

# Bucket used when no rule matches — a network fault, an unmapped provider
# error, or a probe timeout.
_FALLBACK_RULE = _AccessErrorRule(
    bucket=_CONNECTIVITY_UNKNOWN,
    hint=_CONNECTIVITY_HINT,
)

# Every ``error_class`` a failed :class:`ObjectStoreCheckResult` can carry: the
# classifier's rules, its fallback, and the not-configured path that bypasses
# it.  Consumers mapping buckets onto typed errors assert against *this* rather
# than a copy of the literals, so adding a rule without a mapping fails a test
# instead of silently taking the consumer's default branch.
_OBJECT_STORE_ERROR_CLASSES: frozenset[str] = frozenset(
    {rule.bucket for rule in (*_ACCESS_ERROR_RULES, _FALLBACK_RULE)} | {_NOT_CONFIGURED}
)


def _classify_access_error(exc: BaseException) -> tuple[str, str]:
    """Classify an obstore exception into (error_class, remediation_hint).

    Uses obstore's structured exception classes when available; falls back to
    pattern matching on the lowercased error message.  HTTP status codes use
    word-boundary regex (``\\b403\\b``) to avoid false positives from request
    IDs or byte counts that happen to contain those digits.

    Every bucket returned here is in :data:`_OBJECT_STORE_ERROR_CLASSES` by
    construction — both are derived from :data:`_ACCESS_ERROR_RULES`.

    Returns:
        A 2-tuple of (error_class_label, remediation_hint).
    """
    msg = str(exc).lower()

    for rule in _ACCESS_ERROR_RULES:
        if rule.matches(msg):
            return (rule.bucket, rule.hint)

    return (_FALLBACK_RULE.bucket, _FALLBACK_RULE.hint)


@dataclass(frozen=True)
class ObjectStoreCheckResult:
    """Structured outcome of a single object-store access probe.

    Shared by the boot-time path (:func:`verify_object_store_access`, which
    formats these into a raising error string) and the interactive SDR preflight
    path (:func:`check_object_store_access`, which maps these onto UI
    ``PreflightCheck`` rows). One instance per probed store.

    Attributes:
        label: Human-readable role label ("deployment" or "upstream").
        binding_name: The Dapr component name backing the store.
        passed: Whether the write → read round-trip succeeded.
        error_class: Bucket on failure — always one of
            :data:`_OBJECT_STORE_ERROR_CLASSES` (the classifier's buckets, plus
            "not configured" for a binding that was never probed); ``None`` on
            success.
        cause: Concise failure cause (exception text or timeout note); ``None`` on
            success.
        hint: Remediation hint on failure; ``None`` on success.
        failed_operation: Which phase failed ("write", "read/head",
            "connectivity") — used only to reproduce the boot message format;
            ``None`` on success.
    """

    label: str
    binding_name: str
    passed: bool
    error_class: str | None = None
    cause: str | None = None
    hint: str | None = None
    failed_operation: str | None = None

    @property
    def message(self) -> str:
        """A single-line, UI-friendly summary of this result."""
        role = f"{self.label} object store (binding '{self.binding_name}')"
        if self.passed:
            return f"{role} is reachable; read/write access confirmed."
        stage = f"{self.failed_operation} " if self.failed_operation else ""
        detail = f" [{self.error_class}]" if self.error_class else ""
        cause = f": {self.cause}" if self.cause else ""
        return f"{role} {stage}access check failed{detail}{cause}"


async def _probe_store_structured(
    store: object, label: str, binding_name: str
) -> ObjectStoreCheckResult:
    """Run a write → read round-trip against *store*, returning a structured result.

    This is the single shared probe implementation behind both the boot-time
    raising path and the interactive SDR preflight path.  Each obstore operation
    is unbounded here; callers wrap the whole coroutine in ``asyncio.wait_for`` to
    enforce a timeout.

    The probe key is fixed and overwritten on every call — no delete is needed
    or performed.  Environments that prohibit deletes (e.g. an S3 reverse-proxy
    with an intentional no-delete policy) are therefore fully supported.

    Args:
        store: An obstore-compatible store instance.
        label: Human-readable role label ("deployment" or "upstream").
        binding_name: The Dapr component name (used in error messages).

    Returns:
        An :class:`ObjectStoreCheckResult`.  Never raises for obstore-level
        failures — they are captured into the result.
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
        logger.warning(
            "SDR preflight: write probe failed for %s store (binding: %s): %s",
            label,
            binding_name,
            exc,
            exc_info=True,
        )
        error_class, hint = _classify_access_error(exc)
        return ObjectStoreCheckResult(
            label=label,
            binding_name=binding_name,
            passed=False,
            error_class=error_class,
            cause=str(exc),
            hint=hint,
            failed_operation="write",
        )

    # Read (HEAD) — confirms read permission and that the write was committed.
    # The probe key is fixed; we always overwrite it rather than deleting, so
    # no cleanup is needed and delete permission is never required.
    try:
        await obstore.head_async(store, probe_key)
    except Exception as exc:
        logger.warning(
            "SDR preflight: read/head probe failed for %s store (binding: %s): %s",
            label,
            binding_name,
            exc,
            exc_info=True,
        )
        error_class, hint = _classify_access_error(exc)
        return ObjectStoreCheckResult(
            label=label,
            binding_name=binding_name,
            passed=False,
            error_class=error_class,
            cause=str(exc),
            hint=hint,
            failed_operation="read/head",
        )

    return ObjectStoreCheckResult(label=label, binding_name=binding_name, passed=True)


def _format_boot_failure(result: ObjectStoreCheckResult) -> str:
    """Render a failed probe result into the boot-time error block format."""
    return (
        f"  * {result.label} store (binding: '{result.binding_name}'): "
        f"{result.failed_operation} failed [{result.error_class}]\n"
        f"    Cause: {result.cause}\n"
        f"    Hint:  {result.hint}"
    )


async def _probe_store(store: object, label: str, binding_name: str) -> str | None:
    """Boot-time write → read probe returning the preformatted failure string.

    Thin wrapper over :func:`_probe_store_structured` that preserves the boot
    path's historical message format.  The caller wraps this in
    ``asyncio.wait_for`` to enforce the boot-time timeout.

    Returns:
        ``None`` on success; a human-readable failure description on error.
    """
    result = await _probe_store_structured(store, label, binding_name)
    if result.passed:
        return None
    return _format_boot_failure(result)


async def verify_object_store_access(infra: InfrastructureContext) -> None:
    """In SDR mode, verify read+write access to every configured object store.

    Performs a write → read round-trip on the deployment store and, when
    configured, the upstream Atlan store.  The probe key is fixed and
    overwritten on each call — no delete permission is required.  Also
    hard-fails if SDR mode is active but the upstream store is absent — this is
    a defense-in-depth check; the primary guard is
    ``_create_store_from_binding_optional_with_put_attrs`` raising
    ``StorageBindingNotFoundError`` at construction time.

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
            logger.warning(
                "SDR preflight: probe for %s store (binding: %s) timed out after %.0fs",
                label,
                binding_name,
                _PROBE_TIMEOUT_SECS,
                exc_info=True,
            )
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


async def check_object_store_access(
    infra: InfrastructureContext | None,
) -> list[ObjectStoreCheckResult]:
    """Non-raising object-store access probe for the interactive SDR preflight.

    Companion to the boot-time :func:`verify_object_store_access`: both run the
    same write → read round-trip via :func:`_probe_store_structured`, but this
    variant returns structured results instead of raising, so the SDR
    ``preflight_check`` activity can fold them into the handler's
    ``PreflightOutput.checks`` and surface them as UI check rows.

    Probes the deployment store (the customer's own store) and, when present, the
    upstream Atlan upload-proxy store.  Each probe is bounded by
    ``ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS`` (default: 30 s); a timeout is reported as
    a connectivity failure result.

    This function **never raises** and returns ``[]`` immediately when
    ``ENABLE_ATLAN_UPLOAD`` is falsy or *infra* is ``None`` — it is only
    meaningful in Self-Deployed Runtime deployments.

    Args:
        infra: The current ``InfrastructureContext`` (or ``None``).

    Returns:
        One :class:`ObjectStoreCheckResult` per probed store; ``[]`` when SDR
        mode is off or infra is unavailable.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: SDR-gated; constants only loaded when needed
        DEPLOYMENT_OBJECT_STORE_NAME,
        ENABLE_ATLAN_UPLOAD,
        UPSTREAM_OBJECT_STORE_NAME,
    )

    if not ENABLE_ATLAN_UPLOAD or infra is None:
        return []

    stores_to_probe: list[tuple[str, str, object]] = [
        ("deployment", DEPLOYMENT_OBJECT_STORE_NAME, infra.storage),
    ]
    if infra.upstream_storage is not None:
        stores_to_probe.append(
            ("upstream", UPSTREAM_OBJECT_STORE_NAME, infra.upstream_storage)
        )

    results: list[ObjectStoreCheckResult] = []
    for label, binding_name, store in stores_to_probe:
        if store is None:
            results.append(
                ObjectStoreCheckResult(
                    label=label,
                    binding_name=binding_name,
                    passed=False,
                    error_class=_NOT_CONFIGURED,
                    cause=(
                        f"the '{binding_name}' binding is absent or could not be "
                        "parsed, so the store is unavailable"
                    ),
                    hint=(
                        f"Add a Dapr component named '{binding_name}' and ensure its "
                        "credentials are resolvable."
                    ),
                    failed_operation="connectivity",
                )
            )
            continue
        try:
            result = await asyncio.wait_for(
                _probe_store_structured(store, label, binding_name),
                timeout=_PROBE_TIMEOUT_SECS,
            )
        except TimeoutError:
            logger.warning(
                "SDR preflight: probe for %s store (binding: %s) timed out after %.0fs",
                label,
                binding_name,
                _PROBE_TIMEOUT_SECS,
                exc_info=True,
            )
            result = ObjectStoreCheckResult(
                label=label,
                binding_name=binding_name,
                passed=False,
                error_class=_CONNECTIVITY_UNKNOWN,
                cause=f"probe timed out after {_PROBE_TIMEOUT_SECS:.0f}s",
                hint=_CONNECTIVITY_HINT,
                failed_operation="connectivity",
            )
        results.append(result)

    return results
