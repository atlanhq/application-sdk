"""Container-level (cgroup) resource readings for activity sizing telemetry.

``resource_sampler`` reads the **process**: ``/proc/self/stat`` RSS and
``getrusage`` CPU. That is the right instrument for the App Vitals efficiency
metrics, and the wrong one for deciding how big a pod an activity needs.

Three gaps, and each one is a wrong answer rather than a missing one:

1. **RSS is not what the OOM killer acts on.** The kernel kills on the cgroup's
   ``memory.current``, which includes page cache, kernel memory and any child
   process. An activity that shells out, or that reads a large Parquet file
   through the page cache, is under-measured by RSS — in the direction that
   makes a too-small tier look safe.
2. **Start/end point samples miss the peak.** A tier has to cover the *maximum*
   an activity reaches, not its value at the moment it finished. A query that
   builds a 12 GiB hash table and then releases it reads small at both ends.
   ``compute_deltas`` averages the two endpoints, which is right for a
   memory-time integral and useless for sizing.
3. **CPU seconds cannot distinguish "cheap" from "starved".** An activity given
   a 1-core quota and needing 3 will report ~1 core-second per wall second and
   look perfectly sized. ``cpu.stat``'s throttling counters are the only signal
   that separates the two, and they are the reason a CPU tier can be raised on
   evidence instead of on a guess.

Everything here returns ``None`` rather than raising or guessing. cgroup reads
are genuinely fragile — vcluster and some managed runtimes expose a partial
hierarchy — and a missing reading has to stay distinguishable from a zero one:
sizing on a silent 0 would recommend the smallest tier for every activity.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.resource_sampler import parse_pod_memory_limit

_logger = get_logger(__name__)

# cgroup v2 first, then the v1 hierarchy. Order matters: a host running v2 with
# the v1 compatibility mounts still present must read the v2 values, because
# those are the ones the kernel is enforcing.
_MEMORY_CURRENT_PATHS = (
    "/sys/fs/cgroup/memory.current",
    "/sys/fs/cgroup/memory/memory.usage_in_bytes",
)
_MEMORY_LIMIT_PATHS = (
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
)
# v2 ``memory.peak`` landed in 5.19 and became writable (resettable) in 6.8.
# v1 ``memory.max_usage_in_bytes`` has always been resettable by writing 0.
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

# v1 writes "no limit" as a near-2**63 sentinel rather than a word. Anything at
# or above this is "unlimited", which for sizing purposes means "unknown" — the
# pod is bounded by the node, and the node's size is not this activity's budget.
_V1_UNLIMITED_FLOOR = 1 << 62


def _read_first(paths: tuple[str, ...]) -> str | None:
    """Contents of the first readable path, stripped, or ``None``."""
    for path in paths:
        try:
            with open(path) as fh:
                return fh.read().strip()
        # conformance: ignore[E004] a missing cgroup file is the expected case off
        # Linux and under partial hierarchies; None is the meaningful answer.
        except OSError:
            continue
    return None


def _read_int(paths: tuple[str, ...]) -> int | None:
    """First readable path parsed as a non-negative int, or ``None``."""
    for path in paths:
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        # conformance: ignore[E004] see _read_first; a partial hierarchy must not raise.
        except OSError:
            continue
        try:
            value = int(raw)
        except ValueError:
            # "max" (v2 unlimited) lands here, as does an empty file. Both mean
            # "no usable number", and both should let a later path be tried.
            continue
        if value >= 0:
            return value
    return None


def memory_usage_bytes() -> int | None:
    """Current container memory usage, or ``None`` if unreadable.

    This is the counter the kernel OOM killer acts on, which is why it — not
    RSS — is the number a memory tier has to cover.
    """
    return _read_int(_MEMORY_CURRENT_PATHS)


def memory_limit_bytes() -> int | None:
    """The container's memory limit in bytes, or ``None``.

    Prefers the cgroup, then falls back to ``K8S_POD_MEMORY_LIMIT`` — the env var
    the rest of the SDK already reads (``main``, ``execution.heartbeat``). The
    cgroup comes first because it reflects what the kernel is enforcing; the env
    var reflects what the manifest asked for, and they diverge whenever a
    mutating admission controller or a VPA is in play.

    ``None`` for an unlimited cgroup: an unbounded container is bounded only by
    its node, and a node's size is not this activity's budget.
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
    """The kernel's high-water mark for this cgroup, or ``None``.

    Cumulative since the cgroup was created **or since the last successful
    reset**. On a worker that runs activities back to back the un-reset value is
    a pod-lifetime watermark and therefore not attributable to any one activity
    — which is exactly what :func:`reset_memory_peak` exists to fix.
    """
    return _read_int(_MEMORY_PEAK_PATHS)


def reset_memory_peak() -> bool:
    """Try to reset the kernel high-water mark. Returns whether it was *proven*.

    A proven reset is worth a lot: it makes per-activity peak memory a **two
    file reads per activity** measurement instead of a background poller ticking
    every second in every worker in the fleet.

    Proof, not version-sniffing. ``memory.peak`` is only writable from Linux 6.8,
    a kernel write can succeed and be a no-op, and the v1/v2 split makes any
    version table unreliable — so this reads the watermark back and requires it
    to have actually dropped. If the watermark is already sitting at current
    usage there is no drop to observe, so the reset is unprovable and this
    returns ``False``; the caller falls back to polling, which is correct but
    more expensive. That is the safe direction: an unproven reset would report
    the pod's lifetime watermark as this activity's peak.
    """
    before = memory_peak_bytes()
    if before is None:
        return False
    current = memory_usage_bytes()
    if current is None or before <= current:
        # No headroom between the watermark and current usage, so a successful
        # reset would be indistinguishable from a silent no-op.
        return False
    for path in _MEMORY_PEAK_PATHS:
        try:
            with open(path, "w") as fh:
                fh.write("0")
        # conformance: ignore[E004] read-only on <6.8 and absent off Linux; both
        # are expected and mean "fall back to polling".
        except OSError:
            continue
        after = memory_peak_bytes()
        if after is not None and after < before:
            return True
    return False


@dataclass(frozen=True)
class CpuStat:
    """A point-in-time read of the container's CPU accounting.

    ``throttled_seconds`` is the one that matters for sizing: it is wall-clock
    time during which runnable threads were held off the CPU because the quota
    was exhausted. Unlike ``usage_seconds`` it cannot be confused with an
    activity that is simply cheap.
    """

    usage_seconds: float
    nr_periods: int
    nr_throttled: int
    throttled_seconds: float


def cpu_stat() -> CpuStat | None:
    """Read container CPU usage and throttling, or ``None`` if unreadable.

    v2 exposes everything in one ``cpu.stat`` in microseconds. v1 splits usage
    into ``cpuacct.usage`` (nanoseconds) and throttling into ``cpu/cpu.stat``
    (``throttled_time``, nanoseconds), so the two halves are read separately and
    a v1 host missing ``cpuacct`` still yields the throttling counters.
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
        except ValueError:
            continue
    return out


def cpu_quota_cores() -> float | None:
    """The container's CPU limit in cores, or ``None`` if unlimited/unreadable.

    The denominator for "was this activity starved": throttling only makes sense
    against the quota that caused it.
    """
    raw = _read_first((_CPU_MAX_V2,))
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = float(parts[0]), float(parts[1])
            except ValueError:
                quota = period = 0.0
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
    """What one activity actually consumed, as measured at the container.

    Deliberately records ``peak_source``. A peak from a reset kernel watermark
    and a peak from a 1-second poller are not the same measurement — the poller
    misses any spike shorter than its interval — and a tier derived from the two
    mixed together would be tuned to an unknown blend of the two error profiles.
    """

    peak_memory_bytes: int | None = None
    peak_memory_fraction: float | None = None
    peak_source: str = "unavailable"
    memory_limit_bytes: int | None = None
    cpu_seconds: float | None = None
    cpu_throttled_seconds: float | None = None
    cpu_throttled_periods: int | None = None
    cpu_periods: int | None = None
    cpu_quota_cores: float | None = None
    samples: list[float] = field(default_factory=list)

    def observe(self, used: int | None, limit: int | None) -> None:
        """Fold one memory reading into the peak."""
        if used is None:
            return
        if self.peak_memory_bytes is None or used > self.peak_memory_bytes:
            self.peak_memory_bytes = used
            if limit:
                self.peak_memory_fraction = used / limit

    @property
    def throttled_fraction(self) -> float | None:
        """Share of scheduling periods in which the quota was exhausted.

        The headline CPU-starvation number. Above roughly 0.1 the activity spent
        a tenth of its life waiting for CPU it was entitled to ask for, which is
        a tier problem rather than a code problem.
        """
        if not self.cpu_periods or self.cpu_throttled_periods is None:
            return None
        return self.cpu_throttled_periods / self.cpu_periods


@contextlib.asynccontextmanager
async def track_container_usage(
    poll_interval_seconds: float = 1.0,
) -> AsyncIterator[ContainerTrace]:
    """Measure peak memory and CPU throttling across a block.

    Picks the cheapest instrument that works:

    * **watermark** — the kernel's ``memory.peak`` is reset on entry and read on
      exit. Two reads per activity, and it catches spikes of *any* duration.
    * **poll** — a background task samples ``memory.current``. Used only when the
      watermark could not be reset, because an un-reset watermark reports the
      pod's whole life rather than this activity.
    * **unavailable** — no cgroup at all (macOS, most local dev). The block runs
      untouched and the trace stays ``None``, which downstream must read as "no
      data" and not as zero.

    Never raises, and never fails the wrapped block: this is telemetry, and an
    activity that succeeded must not be turned into a failure by the thing
    measuring it. Note the contrast with AE's ``report_memory_pressure``, whose
    watchdog raises *on purpose* — that one is a safety device, this one is an
    instrument.

    ``poll_interval_seconds <= 0`` disables polling entirely, so a fleet-wide
    rollout can take the free watermark path and nothing else.
    """
    trace = ContainerTrace()
    start_cpu: CpuStat | None = None
    task: asyncio.Task[None] | None = None

    # Guarded as a whole, not read by read. Every line below touches the cgroup
    # or the event loop, and the block it wraps has to run either way — an
    # unguarded setup read is the one place this instrument could still fail the
    # activity it is measuring, which a test now pins.
    try:
        trace.memory_limit_bytes = memory_limit_bytes()
        start_cpu = cpu_stat()
        trace.cpu_quota_cores = cpu_quota_cores()

        if memory_usage_bytes() is None:
            trace.peak_source = "unavailable"
        elif reset_memory_peak():
            trace.peak_source = "watermark"
        elif poll_interval_seconds > 0:
            trace.peak_source = "poll"

            async def _poll() -> None:
                while True:
                    await asyncio.sleep(poll_interval_seconds)
                    used = memory_usage_bytes()
                    trace.observe(used, trace.memory_limit_bytes)
                    if used is not None and trace.memory_limit_bytes:
                        trace.samples.append(used / trace.memory_limit_bytes)

            task = asyncio.create_task(_poll())
        else:
            trace.peak_source = "unavailable"
    except Exception:
        _logger.debug("container usage setup failed", exc_info=True)

    try:
        yield trace
    finally:
        if task is not None:
            task.cancel()
            # Suppresses Exception too, not just CancelledError: if a cgroup read
            # blew up inside the poller, ``await task`` re-raises it here — on the
            # exit path of an activity that may well have succeeded.
            # conformance: ignore[E004] deliberate; the poller's failure is
            # telemetry loss and must not become the activity's failure.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _finalise(trace, start_cpu)


def _finalise(trace: ContainerTrace, start_cpu: CpuStat | None) -> None:
    """Close out a trace: read the watermark and difference the CPU counters.

    Wrapped whole in a guard for the same reason the sampler is: a telemetry
    read failing at the end of a successful activity must not surface as a
    failure of that activity.
    """
    try:
        if trace.peak_source == "watermark":
            trace.observe(memory_peak_bytes(), trace.memory_limit_bytes)
        elif trace.peak_source == "poll":
            # One final reading, so a block shorter than the poll interval still
            # produces a number instead of a None.
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
    except Exception:
        _logger.debug("container usage finalisation failed", exc_info=True)
