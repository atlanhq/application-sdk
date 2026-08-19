"""What one activity execution consumed — the unit of evidence for tier sizing.

This is the collection half of "collect data → classify tiers → productionise".
It answers one question per activity execution: *how big a pod did this actually
need?* Nothing here decides a tier, and nothing here reads a tier — a module
that both measured and routed would make the measurement a function of the
routing and the calibration circular.

Two OTel histograms go out immediately, so a tenant is observable the day
collection is enabled rather than after an offline analysis round-trip. The
richer per-execution record is what the analysis skill consumes; its wire
format and durable sink are deliberately behind :func:`record_observation` so
they can change without touching the interceptor.

**Labels are bounded on purpose.** ``activity.type`` and ``task_queue`` are
finite per app; ``workflow_id`` is a UUID and would blow up every histogram it
touched. That mistake has already been made once on the AE dashboards, which is
why the rule is written down here rather than left to reviewers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import orjson
from opentelemetry import metrics as _otel_metrics

from application_sdk.observability.cgroup import ContainerTrace
from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)

_METER_NAME = "application_sdk.sizing"
_INSTRUMENTS: dict[str, Any] = {}

_MIB = 1024 * 1024


@dataclass(frozen=True)
class SizingObservation:
    """One activity execution's measured resource envelope.

    ``peak_source`` travels with the numbers rather than being dropped after
    collection. A peak from a reset kernel watermark and a peak from a 1-second
    poller have different blind spots — the poller cannot see a sub-second spike
    — so a tier fitted to a silent mix of the two is fitted to an unknown error
    profile. The analysis stage needs to be able to segment on it.

    ``attempt`` matters for the same reason: a retry that inherited a warm page
    cache is not an independent sample of the same workload.
    """

    activity_type: str
    task_queue: str
    workflow_type: str
    attempt: int
    outcome: str
    duration_seconds: float

    peak_memory_bytes: int | None = None
    peak_memory_fraction: float | None = None
    peak_source: str = "unavailable"
    memory_limit_bytes: int | None = None

    cpu_seconds: float | None = None
    cpu_throttled_seconds: float | None = None
    cpu_throttled_fraction: float | None = None
    cpu_quota_cores: float | None = None

    @property
    def mean_cpu_cores(self) -> float | None:
        """CPU consumed per wall-clock second.

        The number a CPU tier is set from, and only half the story on its own —
        read it next to ``cpu_throttled_fraction``, because an activity pinned at
        its quota reports a flattering mean precisely *because* it was starved.
        """
        if self.cpu_seconds is None or self.duration_seconds <= 0:
            return None
        return self.cpu_seconds / self.duration_seconds

    @classmethod
    def from_trace(
        cls,
        trace: ContainerTrace,
        *,
        activity_type: str,
        task_queue: str,
        workflow_type: str,
        attempt: int,
        outcome: str,
        duration_seconds: float,
    ) -> SizingObservation:
        return cls(
            activity_type=activity_type,
            task_queue=task_queue,
            workflow_type=workflow_type,
            attempt=attempt,
            outcome=outcome,
            duration_seconds=duration_seconds,
            peak_memory_bytes=trace.peak_memory_bytes,
            peak_memory_fraction=trace.peak_memory_fraction,
            peak_source=trace.peak_source,
            memory_limit_bytes=trace.memory_limit_bytes,
            cpu_seconds=trace.cpu_seconds,
            cpu_throttled_seconds=trace.cpu_throttled_seconds,
            cpu_throttled_fraction=trace.throttled_fraction,
            cpu_quota_cores=trace.cpu_quota_cores,
        )

    def has_data(self) -> bool:
        """Whether anything was actually measured.

        An observation with no peak and no CPU delta is what a non-cgroup host
        produces. Emitting it would put a row of nulls into the dataset the tier
        table is fitted from, and a null read as a zero picks the smallest tier.
        """
        return self.peak_memory_bytes is not None or self.cpu_seconds is not None


def _meter():
    return _otel_metrics.get_meter(_METER_NAME)


def _peak_memory_mib():
    if "peak_mem" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_mem"] = _meter().create_histogram(
            "activity.sizing.peak_memory_mib",
            unit="MiBy",
            description=(
                "Peak container memory (cgroup memory.current / memory.peak) "
                "observed during one activity execution. This is the counter the "
                "kernel OOM killer acts on, so it — not process RSS — is what a "
                "memory tier has to cover."
            ),
        )
    return _INSTRUMENTS["peak_mem"]


def _peak_memory_fraction():
    if "peak_frac" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_frac"] = _meter().create_histogram(
            "activity.sizing.peak_memory_fraction",
            unit="1",
            description=(
                "Peak container memory as a fraction of the container limit. The "
                "utilisation view: sustained low values mean an over-provisioned "
                "tier, values near 1.0 mean the next run may OOM."
            ),
        )
    return _INSTRUMENTS["peak_frac"]


def _cpu_throttled_fraction():
    if "throttle" not in _INSTRUMENTS:
        _INSTRUMENTS["throttle"] = _meter().create_histogram(
            "activity.sizing.cpu_throttled_fraction",
            unit="1",
            description=(
                "Share of CFS periods in which the activity's container exhausted "
                "its CPU quota. The only signal that separates a cheap activity "
                "from a starved one — mean CPU cannot, because a throttled "
                "activity reports a mean pinned neatly at its quota."
            ),
        )
    return _INSTRUMENTS["throttle"]


def _mean_cpu_cores():
    if "cpu_cores" not in _INSTRUMENTS:
        _INSTRUMENTS["cpu_cores"] = _meter().create_histogram(
            "activity.sizing.mean_cpu_cores",
            unit="1",
            description=(
                "Container CPU seconds consumed per wall-clock second during one "
                "activity execution. Read alongside cpu_throttled_fraction."
            ),
        )
    return _INSTRUMENTS["cpu_cores"]


def record_observation(observation: SizingObservation) -> None:
    """Emit one observation: OTel histograms now, durable record for analysis.

    Never raises. This is called from an activity's ``finally``, where an
    exception would replace the activity's real outcome — success or a genuine
    failure — with a telemetry bug.

    The structured log line is the sink that works on every tenant today,
    because every tenant already ships worker logs. A durable columnar sink is a
    separate change; it goes behind this same function so the interceptor never
    learns where the data lands.
    """
    if not observation.has_data():
        return
    try:
        attrs = {
            "activity.type": observation.activity_type,
            "temporal.task_queue": observation.task_queue,
            "outcome": observation.outcome,
            "peak.source": observation.peak_source,
        }
        if observation.peak_memory_bytes is not None:
            _peak_memory_mib().record(observation.peak_memory_bytes / _MIB, attrs)
        if observation.peak_memory_fraction is not None:
            _peak_memory_fraction().record(observation.peak_memory_fraction, attrs)
        if observation.cpu_throttled_fraction is not None:
            _cpu_throttled_fraction().record(observation.cpu_throttled_fraction, attrs)
        mean_cores = observation.mean_cpu_cores
        if mean_cores is not None:
            _mean_cpu_cores().record(mean_cores, attrs)

        payload = asdict(observation)
        payload["mean_cpu_cores"] = mean_cores
        # One JSON object on one line, so a log pipeline can lift the whole
        # dataset out with a single grep on the marker below.
        _logger.info(
            "activity_sizing_observation %s",
            orjson.dumps(payload, default=str).decode(),
        )
    # conformance: ignore[E004] telemetry in an activity finally; a failure here must cost the observation, never the activity's real outcome
    except Exception:
        _logger.debug("sizing observation emission failed", exc_info=True)
