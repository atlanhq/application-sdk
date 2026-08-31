"""Container (cgroup) readings for sizing: the OOM killer acts on the container,
not the process. Readers return ``None``, never a guessed zero.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.resource_sampler import parse_pod_memory_limit

_logger = get_logger(__name__)

# v2 first: a host with both mounted must read v2, which is what the kernel enforces.
_MEMORY_CURRENT_PATHS = (
    "/sys/fs/cgroup/memory.current",
    "/sys/fs/cgroup/memory/memory.usage_in_bytes",
)
_MEMORY_LIMIT_PATHS = (
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
)
# v2 memory.peak: readable from 5.19, writable from 6.8. v1 always writable.
_MEMORY_PEAK_PATHS = (
    "/sys/fs/cgroup/memory.peak",
    "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",
)
_CPU_STAT_V2 = "/sys/fs/cgroup/cpu.stat"
_CPU_STAT_V1 = "/sys/fs/cgroup/cpu/cpu.stat"
_CPUACCT_USAGE_V1 = "/sys/fs/cgroup/cpuacct/cpuacct.usage"
_CPU_MAX_V2 = "/sys/fs/cgroup/cpu.max"
_CPU_QUOTA_V1 = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CPU_PERIOD_V1 = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

# v1 spells "no limit" as a near-2**63 sentinel; unlimited means unknown here.
_V1_UNLIMITED_FLOOR = 1 << 62


def _read_first(paths: tuple[str, ...]) -> str | None:
    """Contents of the first readable path, stripped, or ``None``."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        # conformance: ignore[E014] a missing cgroup file is the expected case off Linux and under partial hierarchies; None is the meaningful answer, and logging per read would fire on every call
        except OSError:
            continue
    return None


def _read_int(paths: tuple[str, ...]) -> int | None:
    """First readable path parsed as a non-negative int, or ``None``."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        # conformance: ignore[E014] see _read_first; a partial hierarchy must not raise and must not log per read
        except OSError:
            continue
        try:
            value = int(raw)
        # conformance: ignore[E014] "max" (v2 unlimited) and an empty file both land here; both mean "no usable number" and must let a later path be tried
        except ValueError:
            continue
        if value >= 0:
            return value
    return None


def memory_usage_bytes() -> int | None:
    """Current container memory usage — the counter the OOM killer acts on."""
    return _read_int(_MEMORY_CURRENT_PATHS)


def memory_limit_bytes() -> int | None:
    """Memory limit, or ``None``. cgroup first (enforced) then the env var
    (requested) — they diverge under a VPA or mutating webhook.
    """
    value = _read_int(_MEMORY_LIMIT_PATHS)
    if value is not None and 0 < value < _V1_UNLIMITED_FLOOR:
        return value
    env = parse_pod_memory_limit(os.environ.get("K8S_POD_MEMORY_LIMIT", ""))
    return env if env > 0 else None


def memory_fraction() -> float | None:
    """Usage as a fraction of the limit, or ``None`` if either side is unknown."""
    limit = memory_limit_bytes()
    if not limit or limit <= 0:
        return None
    used = memory_usage_bytes()
    if used is None:
        return None
    return used / limit


def memory_peak_bytes() -> int | None:
    """Kernel high-water mark. Un-reset it is pod-lifetime, so attributable to
    no single activity — see :func:`reset_memory_peak`.
    """
    return _read_int(_MEMORY_PEAK_PATHS)


def reset_memory_peak() -> bool:
    """Reset the high-water mark; ``True`` only if *proven* by reading it back.
    A write can silently no-op, and an unproven reset reports a lifetime peak.
    """
    before = memory_peak_bytes()
    if before is None:
        return False
    current = memory_usage_bytes()
    if current is None or before <= current:
        # No headroom, so a real reset is indistinguishable from a no-op.
        return False
    for path in _MEMORY_PEAK_PATHS:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("0")
        # conformance: ignore[E014] read-only before Linux 6.8 and absent off Linux; both are expected and mean "fall back to polling"
        except OSError:
            continue
        after = memory_peak_bytes()
        if after is not None and after < before:
            return True
    return False


def prepare_memory_watermark(current: int | None = None) -> bool:
    """Whether ``memory.peak`` can later be read as this block's peak: either a
    stale peak was cleared and proven, or there was nothing stale to clear.
    """
    before = memory_peak_bytes()
    if before is None:
        return False
    if current is None:
        current = memory_usage_bytes()
        if current is None:
            return False
    if before <= current:
        return True
    return reset_memory_peak()


@dataclass(frozen=True)
class CpuStat:
    """Point-in-time CPU accounting. ``throttled_seconds`` is the sizing signal —
    unlike ``usage_seconds`` it cannot be mistaken for cheapness.
    """

    usage_seconds: float
    nr_periods: int
    nr_throttled: int
    throttled_seconds: float


def cpu_stat() -> CpuStat | None:
    """CPU usage and throttling, or ``None``. v1 splits the two across files,
    read separately so a missing ``cpuacct`` still yields throttling.
    """
    raw = _read_first((_CPU_STAT_V2,))
    if raw:
        fields = _parse_kv(raw)
        usage_usec = fields.get("usage_usec")
        if usage_usec is not None:
            return CpuStat(
                usage_seconds=usage_usec / 1e6,
                nr_periods=int(fields.get("nr_periods", 0)),
                nr_throttled=int(fields.get("nr_throttled", 0)),
                throttled_seconds=fields.get("throttled_usec", 0) / 1e6,
            )

    raw = _read_first((_CPU_STAT_V1,))
    if raw is None:
        return None
    fields = _parse_kv(raw)
    usage_ns = _read_int((_CPUACCT_USAGE_V1,))
    return CpuStat(
        usage_seconds=(usage_ns / 1e9) if usage_ns is not None else 0.0,
        nr_periods=int(fields.get("nr_periods", 0)),
        nr_throttled=int(fields.get("nr_throttled", 0)),
        throttled_seconds=fields.get("throttled_time", 0) / 1e9,
    )


def _parse_kv(raw: str) -> dict[str, float]:
    """Parse ``key value`` lines, skipping anything non-numeric."""
    out: dict[str, float] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = float(parts[1])
        # conformance: ignore[E014] cpu.stat gains keys across kernel versions and some carry non-numeric values; skipping one malformed line must not lose the rest of the file, and there is nothing to report — the caller sees the key as absent
        except ValueError:
            continue
    return out


def cpu_quota_cores() -> float | None:
    """CPU limit in cores, or ``None``. The denominator for throttling."""
    raw = _read_first((_CPU_MAX_V2,))
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = float(parts[0]), float(parts[1])
            except ValueError:
                _logger.debug("unparseable cpu.max: %r", raw, exc_info=True)
            else:
                if quota > 0 and period > 0:
                    return quota / period

    quota_us = _read_int((_CPU_QUOTA_V1,))
    period_us = _read_int((_CPU_PERIOD_V1,))
    # v1 writes -1 for unlimited, which _read_int already rejects as negative.
    if quota_us and period_us and quota_us > 0 and period_us > 0:
        return quota_us / period_us
    return None


@dataclass
class ContainerTrace:
    """What one activity consumed. ``peak_source`` is recorded because watermark
    and polled peaks have different blind spots and must not be pooled blindly.
    """

    peak_memory_bytes: int | None = None
    peak_memory_fraction: float | None = None
    peak_source: str = "unavailable"
    memory_limit_bytes: int | None = None
    #: Memory already resident at entry. ``memory.current`` is pod-wide and
    #: cumulative, so the Nth activity starts from what the (N-1)th left pooled.
    start_memory_bytes: int | None = None
    cpu_seconds: float | None = None
    cpu_throttled_seconds: float | None = None
    cpu_throttled_periods: int | None = None
    cpu_periods: int | None = None
    cpu_quota_cores: float | None = None

    def observe(self, used: int | None, limit: int | None) -> None:
        """Fold one memory reading into the peak."""
        if used is None:
            return
        if self.peak_memory_bytes is None or used > self.peak_memory_bytes:
            self.peak_memory_bytes = used
            if limit:
                self.peak_memory_fraction = used / limit

    @property
    def peak_delta_bytes(self) -> int | None:
        """Peak above the entry baseline — fit on this, provision on the absolute
        peak. Floored at zero, since freeing mid-block can leave peak < start.
        """
        if self.peak_memory_bytes is None or self.start_memory_bytes is None:
            return None
        return max(0, self.peak_memory_bytes - self.start_memory_bytes)

    @property
    def throttled_fraction(self) -> float | None:
        """Share of CFS periods that exhausted the quota — the starvation number."""
        if not self.cpu_periods or self.cpu_throttled_periods is None:
            return None
        return self.cpu_throttled_periods / self.cpu_periods


@contextlib.asynccontextmanager
async def track_container_usage(
    poll_interval_seconds: float = 1.0,
    allow_watermark_reset: bool = True,
) -> AsyncIterator[ContainerTrace]:
    """Measure peak memory and CPU throttling across a block; never raises.
    ``allow_watermark_reset=False`` forces polling (memory.peak is one counter).
    """
    trace = ContainerTrace()
    start_cpu: CpuStat | None = None
    task: asyncio.Task[None] | None = None

    # Guarded as a whole: an unguarded setup read is the one place this could
    # still fail the activity it measures.
    try:
        trace.memory_limit_bytes = memory_limit_bytes()
        start_cpu = cpu_stat()
        trace.cpu_quota_cores = cpu_quota_cores()

        # One read: it is both the cgroup-present test and the baseline, and two
        # reads would sample two different instants.
        trace.start_memory_bytes = memory_usage_bytes()

        if trace.start_memory_bytes is None:
            trace.peak_source = "unavailable"
        elif allow_watermark_reset and prepare_memory_watermark(
            trace.start_memory_bytes
        ):
            trace.peak_source = "watermark"
        elif poll_interval_seconds > 0:
            trace.peak_source = "poll"
            # Seed with the baseline (a real t=0 reading) so peak >= start even
            # for a block shorter than one poll interval.
            trace.observe(trace.start_memory_bytes, trace.memory_limit_bytes)

            async def _poll() -> None:
                while True:
                    await asyncio.sleep(poll_interval_seconds)
                    trace.observe(memory_usage_bytes(), trace.memory_limit_bytes)

            task = asyncio.create_task(_poll())
        else:
            trace.peak_source = "unavailable"
    # conformance: ignore[E004] telemetry setup; an instrument must never fail the block it measures
    except Exception:
        _logger.debug("container usage setup failed", exc_info=True)

    try:
        yield trace
    finally:
        if task is not None:
            task.cancel()
            # Exception too: a failed cgroup read would be re-raised by await.
            # conformance: ignore[E003] deliberate; the poller's failure is telemetry loss and must not become the activity's failure
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _finalise(trace, start_cpu)


def _finalise(trace: ContainerTrace, start_cpu: CpuStat | None) -> None:
    """Read the watermark and difference the CPU counters. Never raises."""
    try:
        if trace.peak_source == "watermark":
            trace.observe(memory_peak_bytes(), trace.memory_limit_bytes)
        elif trace.peak_source == "poll":
            # Final reading, so a block shorter than the interval still yields a number.
            trace.observe(memory_usage_bytes(), trace.memory_limit_bytes)

        end_cpu = cpu_stat()
        if start_cpu is not None and end_cpu is not None:
            trace.cpu_seconds = max(
                0.0, end_cpu.usage_seconds - start_cpu.usage_seconds
            )
            trace.cpu_throttled_seconds = max(
                0.0, end_cpu.throttled_seconds - start_cpu.throttled_seconds
            )
            trace.cpu_throttled_periods = max(
                0, end_cpu.nr_throttled - start_cpu.nr_throttled
            )
            trace.cpu_periods = max(0, end_cpu.nr_periods - start_cpu.nr_periods)
    # conformance: ignore[E004] telemetry finalisation; runs after a successful activity and must not turn it into a failure
    except Exception:
        _logger.debug("container usage finalisation failed", exc_info=True)
