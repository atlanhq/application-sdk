"""What one activity execution consumed. Collection only — a measurement that
read the routing would make the calibration circular. Labels stay bounded.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import orjson

from application_sdk.observability.cgroup import ContainerTrace
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics import create_histogram
from application_sdk.observability.sizing_inputs import InputSize

_logger = get_logger(__name__)

_INSTRUMENTS: dict[str, Any] = {}

_MIB = 1024 * 1024

#: Bump on any field or meaning change; rows outlive the version that wrote them.
#: v2 added attribution fields; v3 added the entry baseline. Do not pool versions.
SIZING_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class SizingObservation:
    """One execution's measured envelope. ``peak_source`` and ``attempt`` travel
    with it: peaks have different blind spots, and a retry is not independent.
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
    start_memory_bytes: int | None = None

    cpu_seconds: float | None = None
    cpu_throttled_seconds: float | None = None
    cpu_throttled_fraction: float | None = None
    cpu_quota_cores: float | None = None

    input_bytes: int | None = None
    input_file_count: int | None = None
    input_basis: str | None = None

    # Attribution context: pod + started_at + duration let analysis rebuild the
    # overlap set afterwards, so nothing tracks who-ran-with-whom in-process.
    started_at: float | None = None
    pod: str | None = None
    concurrency_max: int = 1

    @property
    def mean_cpu_cores(self) -> float | None:
        """CPU per wall-clock second. Read with ``cpu_throttled_fraction``: a
        quota-pinned activity reports a flattering mean *because* it was starved.
        """
        if self.cpu_seconds is None or self.duration_seconds <= 0:
            return None
        return self.cpu_seconds / self.duration_seconds

    @property
    def is_attributable(self) -> bool:
        """Whether the peak is this activity's alone. False means pod-wide — still
        useful, but for a pod envelope rather than an activity's.
        """
        return self.concurrency_max <= 1

    @property
    def peak_delta_bytes(self) -> int | None:
        """Peak above the entry baseline. Fit on this; provision on
        :attr:`peak_memory_bytes`, which is what the OOM killer compares.
        """
        if self.peak_memory_bytes is None or self.start_memory_bytes is None:
            return None
        return max(0, self.peak_memory_bytes - self.start_memory_bytes)

    @property
    def peak_per_input_byte(self) -> float | None:
        """Peak per input byte. Carries the pod's history, so comparable only
        within one pod — use :attr:`delta_per_input_byte` across pods.
        """
        if not self.input_bytes or self.peak_memory_bytes is None:
            return None
        return self.peak_memory_bytes / self.input_bytes

    @property
    def delta_per_input_byte(self) -> float | None:
        """The same ratio without the entry baseline — the multiplier to key a rule
        on. Pooling absolute peaks made two tenants look like they disagreed.
        """
        delta = self.peak_delta_bytes
        if not self.input_bytes or delta is None:
            return None
        return delta / self.input_bytes

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
        input_size: InputSize | None = None,
        started_at: float | None = None,
        pod: str | None = None,
        concurrency_max: int = 1,
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
            start_memory_bytes=trace.start_memory_bytes,
            cpu_seconds=trace.cpu_seconds,
            cpu_throttled_seconds=trace.cpu_throttled_seconds,
            cpu_throttled_fraction=trace.throttled_fraction,
            cpu_quota_cores=trace.cpu_quota_cores,
            input_bytes=input_size.bytes if input_size else None,
            input_file_count=input_size.file_count if input_size else None,
            input_basis=input_size.basis if input_size else None,
            started_at=started_at,
            pod=pod,
            concurrency_max=concurrency_max,
        )

    def has_data(self) -> bool:
        """Whether a *resource* reading was obtained. ``input_bytes`` alone does not
        count: with no peak there is no response variable, so the row cannot be fit.
        """
        return self.peak_memory_bytes is not None or self.cpu_seconds is not None


def _input_mib():
    if "input_mib" not in _INSTRUMENTS:
        _INSTRUMENTS["input_mib"] = create_histogram(
            "activity.sizing.input_mib",
            unit="MiBy",
            description=(
                "Bytes of data handed to one activity execution. The driver "
                "variable: peak memory alone says a tier is wrong, this says what "
                "to key it on."
            ),
        )
    return _INSTRUMENTS["input_mib"]


def _peak_memory_mib():
    if "peak_mem" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_mem"] = create_histogram(
            "activity.sizing.peak_memory_mib",
            unit="MiBy",
            description=(
                "Peak container memory during one activity execution. The counter "
                "the OOM killer acts on, so what a memory tier has to cover."
            ),
        )
    return _INSTRUMENTS["peak_mem"]


def _peak_delta_mib():
    if "peak_delta" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_delta"] = create_histogram(
            "activity.sizing.peak_delta_mib",
            unit="MiBy",
            description=(
                "Peak container memory above what was already resident at entry. "
                "The comparable one: peak_memory_mib carries the pod's earlier "
                "activities, so it varies with pod age rather than workload."
            ),
        )
    return _INSTRUMENTS["peak_delta"]


def _peak_memory_fraction():
    if "peak_frac" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_frac"] = create_histogram(
            "activity.sizing.peak_memory_fraction",
            unit="1",
            description=(
                "Peak container memory as a fraction of the limit. Sustained low "
                "means over-provisioned; near 1.0 means the next run may OOM."
            ),
        )
    return _INSTRUMENTS["peak_frac"]


def _cpu_throttled_fraction():
    if "throttle" not in _INSTRUMENTS:
        _INSTRUMENTS["throttle"] = create_histogram(
            "activity.sizing.cpu_throttled_fraction",
            unit="1",
            description=(
                "Share of CFS periods that exhausted the CPU quota. The only signal "
                "separating a cheap activity from a starved one."
            ),
        )
    return _INSTRUMENTS["throttle"]


def _mean_cpu_cores():
    if "cpu_cores" not in _INSTRUMENTS:
        _INSTRUMENTS["cpu_cores"] = create_histogram(
            "activity.sizing.mean_cpu_cores",
            unit="1",
            description=(
                "Container CPU seconds per wall-clock second. Read alongside "
                "cpu_throttled_fraction."
            ),
        )
    return _INSTRUMENTS["cpu_cores"]


def record_observation(observation: SizingObservation) -> None:
    """Emit one observation as histograms plus a JSON log line. Never raises —
    called from an activity's ``finally``, where it would mask the real outcome.
    """
    if not observation.has_data():
        return
    try:
        attrs = {
            "activity.type": observation.activity_type,
            "temporal.task_queue": observation.task_queue,
            "outcome": observation.outcome,
            "peak.source": observation.peak_source,
            # Without this a dashboard averages per-activity peaks together with
            # pod-wide ones, which is two different quantities under one name.
            "attributable": str(observation.is_attributable).lower(),
        }
        if observation.input_bytes is not None:
            _input_mib().record(
                observation.input_bytes / _MIB,
                {**attrs, "input.basis": observation.input_basis or "unknown"},
            )
        if observation.peak_memory_bytes is not None:
            _peak_memory_mib().record(observation.peak_memory_bytes / _MIB, attrs)
        peak_delta = observation.peak_delta_bytes
        if peak_delta is not None:
            _peak_delta_mib().record(peak_delta / _MIB, attrs)
        if observation.peak_memory_fraction is not None:
            _peak_memory_fraction().record(observation.peak_memory_fraction, attrs)
        if observation.cpu_throttled_fraction is not None:
            _cpu_throttled_fraction().record(observation.cpu_throttled_fraction, attrs)
        mean_cores = observation.mean_cpu_cores
        if mean_cores is not None:
            _mean_cpu_cores().record(mean_cores, attrs)

        payload = asdict(observation)
        # Same derived fields the sink writes, and for the same reason: computed
        # once here so no consumer derives them slightly differently.
        payload["mean_cpu_cores"] = mean_cores
        payload["peak_delta_bytes"] = peak_delta
        payload["delta_per_input_byte"] = observation.delta_per_input_byte
        payload["peak_per_input_byte"] = observation.peak_per_input_byte
        payload["is_attributable"] = observation.is_attributable
        # Versioned here too, not just in the sink: this line reaches the central
        # log store, so it is the copy most analysis reads.
        payload["schema_version"] = SIZING_SCHEMA_VERSION
        # One JSON object per line, so a log pipeline lifts the dataset with one grep.
        _logger.info(
            "activity_sizing_observation %s",
            orjson.dumps(payload, default=str).decode(),
        )

        # Durable copy for fitting: this survives log retention, the line above
        # is what is queryable today.
        from application_sdk.observability.sizing_sink import (  # noqa: PLC0415 — circular: the sink imports this module's record type
            persist,
        )

        persist(observation)
    # conformance: ignore[E004] telemetry in an activity finally; a failure here must cost the observation, never the activity's real outcome
    except Exception:
        _logger.debug("sizing observation emission failed", exc_info=True)
