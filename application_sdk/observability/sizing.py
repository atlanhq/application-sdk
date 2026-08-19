"""What one activity execution consumed — the evidence tier sizing is fitted from.

Collection only: nothing here reads or decides a tier, because a measurement that
depended on the routing would make the calibration circular.

Labels are bounded on purpose — ``workflow_id`` is a UUID and would blow up every
histogram it touched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import orjson
from opentelemetry import metrics as _otel_metrics

from application_sdk.observability.cgroup import ContainerTrace
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.sizing_inputs import InputSize

_logger = get_logger(__name__)

_METER_NAME = "application_sdk.sizing"
_INSTRUMENTS: dict[str, Any] = {}

_MIB = 1024 * 1024


@dataclass(frozen=True)
class SizingObservation:
    """One activity execution's measured resource envelope.

    ``peak_source`` and ``attempt`` travel with the numbers so analysis can segment
    on them: watermark and polled peaks have different blind spots, and a retry on
    a warm page cache is not an independent sample.
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

    input_bytes: int | None = None
    input_file_count: int | None = None
    input_basis: str | None = None

    @property
    def mean_cpu_cores(self) -> float | None:
        """CPU consumed per wall-clock second.

        Read next to ``cpu_throttled_fraction``: an activity pinned at its quota
        reports a flattering mean precisely *because* it was starved.
        """
        if self.cpu_seconds is None or self.duration_seconds <= 0:
            return None
        return self.cpu_seconds / self.duration_seconds

    @property
    def peak_per_input_byte(self) -> float | None:
        """Peak memory per input byte — the ratio a memory tier is fitted from.

        ``None`` without an input size: a peak with no driver variable can size one
        envelope but cannot key a rule.
        """
        if not self.input_bytes or self.peak_memory_bytes is None:
            return None
        return self.peak_memory_bytes / self.input_bytes

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
            input_bytes=input_size.bytes if input_size else None,
            input_file_count=input_size.file_count if input_size else None,
            input_basis=input_size.basis if input_size else None,
        )

    def has_data(self) -> bool:
        """Whether anything was measured. A non-cgroup host produces nothing.

        Emitting an all-null row would put it in the dataset the tier table is
        fitted from, where a null read as a zero picks the smallest tier.
        """
        return self.peak_memory_bytes is not None or self.cpu_seconds is not None


def _input_mib():
    if "input_mib" not in _INSTRUMENTS:
        _INSTRUMENTS["input_mib"] = _meter().create_histogram(
            "activity.sizing.input_mib",
            unit="MiBy",
            description=(
                "Bytes of data handed to one activity execution. The driver "
                "variable: peak memory alone says a tier is wrong, this says what "
                "to key it on."
            ),
        )
    return _INSTRUMENTS["input_mib"]


def _meter():
    return _otel_metrics.get_meter(_METER_NAME)


def _peak_memory_mib():
    if "peak_mem" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_mem"] = _meter().create_histogram(
            "activity.sizing.peak_memory_mib",
            unit="MiBy",
            description=(
                "Peak container memory during one activity execution. The counter "
                "the OOM killer acts on, so what a memory tier has to cover."
            ),
        )
    return _INSTRUMENTS["peak_mem"]


def _peak_memory_fraction():
    if "peak_frac" not in _INSTRUMENTS:
        _INSTRUMENTS["peak_frac"] = _meter().create_histogram(
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
        _INSTRUMENTS["throttle"] = _meter().create_histogram(
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
        _INSTRUMENTS["cpu_cores"] = _meter().create_histogram(
            "activity.sizing.mean_cpu_cores",
            unit="1",
            description=(
                "Container CPU seconds per wall-clock second. Read alongside "
                "cpu_throttled_fraction."
            ),
        )
    return _INSTRUMENTS["cpu_cores"]


def record_observation(observation: SizingObservation) -> None:
    """Emit one observation as OTel histograms plus a structured log line.

    Never raises — called from an activity's ``finally``, where an exception would
    replace the activity's real outcome with a telemetry bug.

    The log line is the sink that works on every tenant today; a columnar sink is a
    separate change, hidden behind this function.
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
        if observation.input_bytes is not None:
            _input_mib().record(
                observation.input_bytes / _MIB,
                {**attrs, "input.basis": observation.input_basis or "unknown"},
            )
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
        # One JSON object per line, so a log pipeline lifts the dataset with one grep.
        _logger.info(
            "activity_sizing_observation %s",
            orjson.dumps(payload, default=str).decode(),
        )
    # conformance: ignore[E004] telemetry in an activity finally; a failure here must cost the observation, never the activity's real outcome
    except Exception:
        _logger.debug("sizing observation emission failed", exc_info=True)
