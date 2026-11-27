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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from application_sdk.common._env import env_float
from application_sdk.errors.base import safe_traceback, sanitize_cause_repr
from application_sdk.observability.logger_adaptor import get_logger

# The access classifier lives in ``storage._classify`` with the package's other
# failure classifiers, so the write path in ``ops`` reads it without importing
# this module. Its names stay importable from here (docs/standards/symbols.md).
from application_sdk.storage._classify import (  # noqa: F401 — re-exported
    _ACCESS_ERROR_RULES,
    _CONNECTIVITY_HINT,
    _CONNECTIVITY_UNKNOWN,
    _FALLBACK_RULE,
    _NOT_CONFIGURED,
    _OBJECT_STORE_ERROR_CLASSES,
    _RE_401,
    _RE_403,
    RELOCATION_BUCKET,
    _AccessErrorRule,
)
from application_sdk.storage._classify import (
    classify_access_error as _classify_access_error,
)

if TYPE_CHECKING:
    from application_sdk.infrastructure.context import InfrastructureContext

__all__ = [
    "check_object_store_access",
    "check_run_storage_access",
    "ObjectStoreCheckResult",
    "verify_object_store_access",
]


logger = get_logger(__name__)

_PREFLIGHT_PREFIX = "artifacts/apps/.atlan-sdk-preflight"
_PREFLIGHT_PAYLOAD = b"atlan-preflight"

# Part size for the multipart-forced probe write. 5 MiB is the S3 minimum part
# size; the probe payload is far smaller, so the whole payload travels as one
# part — the point of the probe is the *initiate* call, not the data volume.
_MULTIPART_PROBE_CHUNK_SIZE = 5 * 1024 * 1024

# Floor for the run-path per-store probe timeout. Below this a real cloud
# round-trip of write + HEAD + multipart initiate/part/complete is doomed to
# time out and would report a healthy store as a connectivity failure. The
# preflight gate's own 15s budget floor means it never asks for a share this
# small; a direct caller that does gets no probe rather than either a false
# failure or a wait past the budget it declared (see
# :func:`check_run_storage_access`).
_RUN_PROBE_FLOOR_SECONDS = 5.0

# Per-store probe timeout.  Keeps a blackholed endpoint from stalling boot
# indefinitely — obstore wraps the Rust object_store crate, whose default
# connect_timeout and request timeout are both None.
_DEFAULT_PROBE_TIMEOUT_SECS = 30.0


def _load_probe_timeout() -> float:
    """``ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS``: positive and finite, else the default.

    Non-finite values are rejected with the malformed ones (warning, default):
    ``inf`` would make the probe wait forever on a blackholed endpoint, which
    is the stall this timeout exists to prevent.
    """
    timeout = env_float(
        "ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS", _DEFAULT_PROBE_TIMEOUT_SECS, minimum=0.0
    )
    if timeout > 0:
        return timeout
    logger.warning(
        "Ignoring env var: ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS=%g is not positive; "
        "using the default %g",
        timeout,
        _DEFAULT_PROBE_TIMEOUT_SECS,
    )
    return _DEFAULT_PROBE_TIMEOUT_SECS


_PROBE_TIMEOUT_SECS: float = _load_probe_timeout()

# Fixed probe key overwritten on every boot — guarantees a single object per
# store with no accumulation.  A hostname-scoped key would create one object
# per unique pod name in k8s and never converge.  Since we always write before
# reading, a stale probe from a previous run cannot produce a false positive.
# Must stay under ``artifacts/apps/`` (or another allowed prefix) — the Kong
# s3proxy plugin enforces an upstream_path_prefixes allowlist and rejects
# anything outside it with 403 code 1009.
_PROBE_KEY = f"{_PREFLIGHT_PREFIX}/probe"


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
            "write-multipart", "connectivity") — used only to reproduce the
            boot message format; ``None`` on success.
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
    store: object,
    label: str,
    binding_name: str,
    *,
    include_multipart_probe: bool = False,
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
        include_multipart_probe: Also force the same payload through the
            **multipart** upload API. A store can accept plain PUTs while
            rejecting multipart initiation — GCS does exactly this for the whole
            window of a bucket relocation — so a simple-write probe alone
            certifies a store that the run's larger artifact uploads cannot
            use: the upload writer switches to multipart once a file exceeds
            its part size (8 MiB by default, deployment-tunable via
            ``ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES``), while smaller files go
            out as plain PUTs. The probe is therefore deliberately *stricter*
            than a small-artifact-only workload's real writes. Off by default
            so the boot-time path keeps its historical behavior: a worker must
            not crash-loop for a temporary relocation it could partially work
            through.

            Note on cost: a probe cancelled mid-initiate (timeout) can leave a
            hidden, unfinished multipart upload behind; providers charge for
            those parts until aborted. The payload is a few bytes, but the
            parts are not cleaned up here and cannot be: ``obstore`` owns the
            upload id inside ``put_async`` and exposes no abort call, so a
            cancelled probe leaves nothing to cancel against. The bucket must
            carry an abort-incomplete-multipart lifecycle rule instead — a
            deployment prerequisite surfaced on
            ``App.preflight_verify_storage`` and in the app docs, asserted
            neither here nor anywhere else in the SDK.

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

    # Same per-write attributes real uploads use (e.g. a binding-declared
    # Storage-Class) so the probe certifies the actual write path and never
    # lands its object in the wrong storage class. Resolution failures degrade
    # to attribute-less writes — the probe must not fail on its own plumbing.
    try:
        from application_sdk.storage.ops import (  # noqa: PLC0415 — lazy: ops imports obstore at module load
            _resolve_put_attributes,
        )

        put_attributes = _resolve_put_attributes(store)  # pyright: ignore[reportArgumentType]
    except Exception:
        # Probing without them can certify a write path real uploads do not
        # take (a different storage class), so this is not DEBUG detail.
        logger.warning(
            "Could not resolve put attributes for the %s store probe; "
            "probing without them",
            label,
            exc_info=True,
        )
        put_attributes = None

    # Write
    try:
        await obstore.put_async(
            store, probe_key, _PREFLIGHT_PAYLOAD, attributes=put_attributes
        )
    except Exception as exc:  # noqa: BLE001 — logged at WARNING with a sanitized cause; exc_info would render a presigned URL verbatim
        # The frames are worth keeping — a TLS or driver-level failure is only
        # diagnosable from them — but exc_info=True renders the cause verbatim
        # into both exception.stacktrace and exception.message, and the logger
        # adaptor redacts neither (_format_exception_stacktrace calls
        # traceback.format_exception directly). An object-store error quotes the
        # request URL, so for a presigned request that is the signature.
        # safe_traceback keeps the frames and strips the secrets. This is the
        # no-traceback boundary L004/E005 exempt by design — arguments flowing
        # through a redaction helper — so it needs no suppression directive.
        logger.warning(
            "SDR preflight: write probe failed for %s store (binding: %s): %s\n%s",
            label,
            binding_name,
            sanitize_cause_repr(exc),
            safe_traceback(exc),
        )
        error_class, hint = _classify_access_error(exc)
        return ObjectStoreCheckResult(
            label=label,
            binding_name=binding_name,
            passed=False,
            error_class=error_class,
            cause=sanitize_cause_repr(exc),
            hint=hint,
            failed_operation="write",
        )

    # Read (HEAD) — confirms read permission and that the write was committed.
    # The probe key is fixed; we always overwrite it rather than deleting, so
    # no cleanup is needed and delete permission is never required.
    try:
        await obstore.head_async(store, probe_key)
    except Exception as exc:  # noqa: BLE001 — logged at WARNING with a sanitized cause and safe traceback
        logger.warning(
            "SDR preflight: read/head probe failed for %s store (binding: %s): %s\n%s",
            label,
            binding_name,
            sanitize_cause_repr(exc),
            safe_traceback(exc),
        )
        error_class, hint = _classify_access_error(exc)
        return ObjectStoreCheckResult(
            label=label,
            binding_name=binding_name,
            passed=False,
            error_class=error_class,
            cause=sanitize_cause_repr(exc),
            hint=hint,
            failed_operation="read/head",
        )

    # Multipart write — forces initiation (POST …?uploads=) with the same tiny
    # payload, exercising the API the run's real uploads use. Last, so a store
    # that fails plain access reports the simpler phase first. Overwrites the
    # same fixed key; a single part carries the whole payload, so this costs one
    # initiate + one part + one complete and transfers a few bytes.
    if include_multipart_probe:
        try:
            await obstore.put_async(
                store,
                probe_key,
                _PREFLIGHT_PAYLOAD,
                use_multipart=True,
                chunk_size=_MULTIPART_PROBE_CHUNK_SIZE,
                attributes=put_attributes,
            )
        except Exception as exc:  # noqa: BLE001 — logged at WARNING with a sanitized cause and safe traceback
            logger.warning(
                "Storage preflight: multipart write probe failed for %s store "
                "(binding: %s): %s\n%s",
                label,
                binding_name,
                sanitize_cause_repr(exc),
                safe_traceback(exc),
            )
            error_class, hint = _classify_access_error(exc)
            return ObjectStoreCheckResult(
                label=label,
                binding_name=binding_name,
                passed=False,
                error_class=error_class,
                cause=sanitize_cause_repr(exc),
                hint=hint,
                failed_operation="write-multipart",
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
            ``_create_infrastructure`` in ``infrastructure/_bootstrap.py``.

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
    # On a single-store deployment the upstream handle is either absent or the
    # same bucket already queued as "deployment" — nothing more to probe.
    if not infra.single_store:
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

    stores_to_probe: list[tuple[str, str, object | None]] = [
        ("deployment", DEPLOYMENT_OBJECT_STORE_NAME, infra.storage),
    ]
    # On a single-store deployment the upstream handle is either absent or the
    # same bucket already queued as "deployment" — nothing more to probe.
    if not infra.single_store:
        stores_to_probe.append(
            ("upstream", UPSTREAM_OBJECT_STORE_NAME, infra.upstream_storage)
        )

    return await _check_stores(stores_to_probe, per_store_timeout=_PROBE_TIMEOUT_SECS)


async def _check_stores(
    stores_to_probe: list[tuple[str, str, object | None]],
    *,
    per_store_timeout: float,
) -> list[ObjectStoreCheckResult]:
    """Shared probe loop behind both non-raising checkers. Never raises.

    One implementation so the SDR surface and the run-path gate cannot diverge
    on policy: a ``None`` store — a binding that is absent or failed to parse —
    is a **failed** ``not configured`` check on both paths (a config gap is the
    one case preflight should certainly catch, not an absence of stores to
    probe), every probe includes the multipart-forced write, and a probe cut by
    *per_store_timeout* reports as a connectivity failure.
    """
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
                _probe_store_structured(
                    store, label, binding_name, include_multipart_probe=True
                ),
                timeout=per_store_timeout,
            )
        except TimeoutError:
            logger.warning(
                "Storage preflight: probe for %s store (binding: %s) timed out "
                "after %.0fs",
                label,
                binding_name,
                per_store_timeout,
                exc_info=True,
            )
            result = ObjectStoreCheckResult(
                label=label,
                binding_name=binding_name,
                passed=False,
                error_class=_CONNECTIVITY_UNKNOWN,
                cause=f"probe timed out after {per_store_timeout:.0f}s",
                hint=_CONNECTIVITY_HINT,
                failed_operation="connectivity",
            )
        results.append(result)

    return results


async def check_run_storage_access(
    infra: InfrastructureContext | None,
    *,
    timeout_seconds: float | None = None,
) -> list[ObjectStoreCheckResult]:
    """Non-raising probe of every store a workflow run will upload artifacts to.

    The run-path sibling of :func:`check_object_store_access`: that one is
    SDR-gated because it backs the SDR test-connection surface, while this one
    serves the pre-extraction preflight gate and therefore probes the
    infrastructure (deployment) store on **every** runtime — an in-cluster run's
    artifact uploads go to exactly that store, and a run that cannot upload its
    transformed output is doomed however healthy the source is (a production RCA
    traced multi-hour extractions dying at the final upload to a condition this
    probe detects in under a second). The upstream Atlan store joins the probe
    set when configured (SDR).

    Probes include the multipart-forced write (see
    :func:`_probe_store_structured`) because artifact uploads above the writer's
    part size (8 MiB default) go out as multipart — a store accepting plain PUTs
    but rejecting multipart initiation (a GCS bucket mid-relocation) must fail
    this check.

    This function **never raises**; probe failures are captured into results.

    Args:
        infra: The current ``InfrastructureContext`` (or ``None``).
        timeout_seconds: Overall budget across all probed stores; each store
            gets an equal share, capped by ``ATLAN_SDR_PREFLIGHT_TIMEOUT_SECS``.
            Honoured as a ceiling: the per-store timeout is never raised above
            the share, so this function does not outlast the budget given. A
            share below :data:`_RUN_PROBE_FLOOR_SECONDS` — too little for a real
            round-trip — skips the probes entirely instead. ``None`` gives each
            store the full per-store default.

    Returns:
        One :class:`ObjectStoreCheckResult` per store the run writes to. ``[]``
        means the probes did not run — either *infra* is ``None`` (no
        infrastructure context at all: unit tests, direct calls) or
        *timeout_seconds* left each store less than
        :data:`_RUN_PROBE_FLOOR_SECONDS`. Both are "storage unverified", never
        "storage healthy"; the preflight gate renders an empty result as a
        failed advisory row for exactly that reason. A live context whose
        deployment store is ``None``
        — a binding that is absent or failed to parse — yields a **failed**
        ``not configured`` check, exactly like :func:`check_object_store_access`:
        that config gap is the one case preflight should certainly catch.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: probe-time only
        DEPLOYMENT_OBJECT_STORE_NAME,
        UPSTREAM_OBJECT_STORE_NAME,
    )

    if infra is None:
        return []

    stores_to_probe: list[tuple[str, str, object | None]] = [
        ("deployment", DEPLOYMENT_OBJECT_STORE_NAME, infra.storage),
    ]
    # On a single-store deployment the upstream handle is either absent or the
    # same bucket already queued as "deployment" — nothing more to probe.
    if not infra.single_store:
        stores_to_probe.append(
            ("upstream", UPSTREAM_OBJECT_STORE_NAME, infra.upstream_storage)
        )

    per_store = _PROBE_TIMEOUT_SECS
    if timeout_seconds is not None:
        share = timeout_seconds / len(stores_to_probe)
        if share < _RUN_PROBE_FLOOR_SECONDS:
            # Neither available answer is truthful here, so don't give one.
            # Raising the per-store timeout to the floor would make this
            # function outlast the budget its own signature advertises;
            # probing inside the share would time out and report a healthy
            # store as a connectivity failure. Skip instead — the gate turns
            # an empty result into a visible "storage not verified" row.
            logger.warning(
                "Skipping run-path storage probes: a %.1fs budget across %d "
                "store(s) leaves %.1fs each, under the %.1fs a real round-trip "
                "needs; storage is unverified for this run",
                timeout_seconds,
                len(stores_to_probe),
                share,
                _RUN_PROBE_FLOOR_SECONDS,
            )
            return []
        per_store = min(per_store, share)

    return await _check_stores(stores_to_probe, per_store_timeout=per_store)
