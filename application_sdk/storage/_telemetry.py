"""Transfer telemetry rail — classification, metrics, and progress heartbeats.

Split out of ``storage/ops.py`` so the telemetry surface (what gets counted,
labelled, and logged about a transfer) evolves independently of the I/O
primitives that emit it. ``_log_storage_event`` stays in ``ops`` — it is the
per-attempt event emitter the I/O functions call — and mirrors terminal
transfer events into :func:`_record_transfer_metric` here. (BLDX-1513)
"""

from __future__ import annotations

import logging

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _throughput_mbps(size_bytes: int, elapsed_ms: float) -> float | None:
    """Return MiB/s throughput, or ``None`` when unknown / instantaneous."""
    if elapsed_ms <= 0 or size_bytes <= 0:
        return None
    return round((size_bytes / (1024 * 1024)) / (elapsed_ms / 1000.0), 3)


def _classify_transfer_error(error_class: str | None, store_path: str) -> str:
    """Bucket a transfer failure into a coarse, queryable family.

    Timeouts (slow egress / stalled connections), not-found, permission, and
    everything else fail for different reasons and want different responses —
    a timeout says "look at throughput / the network", a permission error says
    "look at credentials". Pairing this with ``size_bytes`` on the failure event
    also separates a *stall* (≈0 bytes before a timeout) from *slow-but-moving*
    (many bytes before a timeout). (BLDX-1513)
    """
    if error_class is None:
        return "none"
    name = error_class.lower()
    # obstore surfaces both the overall-request timeout and a read_timeout stall
    # as a GenericError whose text mentions timeout; we can't split them from the
    # class name alone, so both land in "timeout" and size_bytes disambiguates.
    if "timeout" in name or "timedout" in name:
        return "timeout"
    if "notfound" in name or "not_found" in name:
        return "not_found"
    if "generic" in name:
        # GenericError is obstore's catch-all; the timeout variant is by far the
        # most common large-transfer failure, but don't assume — label it generic.
        return "generic"
    return "other"


def _record_transfer_metric(
    op: str,
    *,
    outcome: str,
    elapsed_ms: float | None,
    size_bytes: int | None,
    throughput_mibps: float | None,
    error_class: str | None,
) -> None:
    """Emit transfer metrics so throughput / failures are dashboardable + alertable.

    Best-effort: a missing or misconfigured metrics backend must never break a
    transfer. Metrics land on the same OTel / Prometheus / object-store rails as
    the rest of the SDK. The throughput histogram is the fleet-wide signal for
    "this tenant's egress is slow" — alert on it and a slow tenant surfaces
    before its workflows start failing, rather than after. (BLDX-1513)
    """
    try:
        from application_sdk.observability.metrics_adaptor import (  # noqa: PLC0415 — lazy: avoid import-time cycle observability<->storage
            MetricType,
            get_metrics,
        )

        metrics = get_metrics()
        family = _classify_transfer_error(error_class, "")
        labels: dict[str, str | int | float | bool] = {
            "storage_op": op,
            "outcome": outcome,
            "error_family": family,
        }
        metrics.record_metric(
            name="storage_transfer_total",
            value=1,
            metric_type=MetricType.COUNTER,
            labels=labels,
            description="Object-store transfers by op/outcome/error family",
            unit="count",
        )
        if size_bytes is not None and size_bytes >= 0:
            metrics.record_metric(
                name="storage_transfer_bytes",
                value=size_bytes,
                metric_type=MetricType.COUNTER,
                labels={"storage_op": op, "outcome": outcome},
                description="Bytes transferred to/from object store",
                unit="By",
            )
        if elapsed_ms is not None and elapsed_ms >= 0:
            metrics.record_metric(
                name="storage_transfer_duration_ms",
                value=round(elapsed_ms, 3),
                metric_type=MetricType.HISTOGRAM,
                labels={"storage_op": op, "outcome": outcome},
                description="Object-store transfer wall-clock duration",
                unit="ms",
            )
        if throughput_mibps is not None:
            metrics.record_metric(
                name="storage_transfer_throughput_mibps",
                value=throughput_mibps,
                metric_type=MetricType.HISTOGRAM,
                labels={"storage_op": op, "outcome": outcome},
                description="Object-store transfer throughput (MiB/s) — alert when p50 is abnormally low",
                unit="MiBy/s",
            )
    # conformance: ignore[E004] metrics are best-effort telemetry; a backend failure must never break a transfer — logged at debug and swallowed
    except Exception:
        logger.debug("Failed to record transfer metric (best-effort)", exc_info=True)


def _log_transfer_progress(
    op: str,
    store_path: str,
    *,
    bytes_so_far: int,
    elapsed_ms: float,
    total_bytes: int | None = None,
) -> None:
    """Emit an in-progress heartbeat for a long-running upload / download.

    Answers "is this transfer stuck or just slow?" while it is still running —
    the per-attempt success/failure event only lands at the end. Only keys in
    ``_KNOWN_EXTRA_KEYS`` are placed on ``extra`` (so they promote to OTLP
    attributes); the human-readable total / percentage stays in the message
    text. (BLDX-1513)
    """
    extra: dict[str, object] = {
        "storage_op": op,
        "store_path": store_path,
        "outcome": "in_progress",
        "size_bytes": bytes_so_far,
        "elapsed_ms": round(elapsed_ms, 3),
    }
    tput = _throughput_mbps(bytes_so_far, elapsed_ms)
    if tput is not None:
        extra["throughput_mibps"] = tput
    if total_bytes:
        pct = 100.0 * bytes_so_far / total_bytes
        progress = f"{bytes_so_far}/{total_bytes} bytes ({pct:.0f}%)"
    else:
        progress = f"{bytes_so_far} bytes"
    msg = f"storage.{op} in_progress path={store_path} {progress}"
    logger.log(logging.INFO, msg, **extra)
