"""Process-level resource sampling for App Vitals efficiency metrics.

Captures CPU time and memory usage at activity boundaries so the
interceptor can emit ``temporal.activity.cpu_seconds`` and
``temporal.activity.mem_gib_seconds``.

Uses stdlib ``resource`` module — no external dependencies (psutil removed).
On Linux, reads ``/proc/self/stat`` for RSS when available. Falls back to
``resource.getrusage()`` on macOS and other platforms.

Attribution: the samples are **process-level** — the OS cannot say which
activity burned a CPU second when several share one process. Differencing two
samples over one activity's window (:func:`compute_deltas`) therefore charges
that activity for everything every concurrent activity did, and summing those
values over-counts by the concurrency factor. :class:`ActivityShareLedger`
attributes instead: at every activity start and end it samples the process and
splits the consumption since the previous boundary evenly across the
activities in flight. The per-activity values are a fair share, not a precise
measurement, but they add up to what the process consumed while busy and so
never exceed the process total.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass

from application_sdk.observability.logger_adaptor import get_logger

_logger = get_logger(__name__)


@dataclass(frozen=True)
class ResourceSample:
    """A single point-in-time sample of process CPU + memory."""

    cpu_time_s: float  # user + system CPU seconds consumed by the process so far
    rss_bytes: int  # resident set size in bytes (memory currently in RAM)


_BINARY_SUFFIXES: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_DECIMAL_SUFFIXES: dict[str, int] = {
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def parse_pod_memory_limit(raw: str) -> int:
    """Parse a K8S_POD_MEMORY_LIMIT value into bytes.

    Accepts plain integer bytes (the value produced by ``resourceFieldRef``
    with ``divisor: "1"``) and Kubernetes quantity suffixes:

    * Binary: ``Ki``, ``Mi``, ``Gi``, ``Ti``, ``Pi``, ``Ei``
    * Decimal: ``k``, ``M``, ``G``, ``T``, ``P``, ``E``

    Returns 0 on any parse failure so callers can safely treat 0 as
    "limit unknown / feature disabled".
    """
    raw = raw.strip()
    if not raw:
        return 0
    for suffix, multiplier in {**_BINARY_SUFFIXES, **_DECIMAL_SUFFIXES}.items():
        if raw.endswith(suffix):
            try:
                return int(raw[: -len(suffix)]) * multiplier
            except ValueError as e:
                _logger.warning(
                    "Could not parse pod memory limit %r (suffix %r): %s",
                    raw,
                    suffix,
                    e,
                    exc_info=True,
                )
                return 0
    try:
        return int(raw)
    except ValueError as e:
        _logger.warning(
            "Could not parse pod memory limit %r as integer bytes: %s",
            raw,
            e,
            exc_info=True,
        )
        return 0


def _read_proc_rss() -> int | None:
    """Read RSS from /proc/self/stat (Linux only, no dependencies)."""
    try:
        with open("/proc/self/stat", "rb") as f:
            parts = f.read().split()
        # Field 24 (index 23) is RSS in pages
        rss_pages = int(parts[23])
        page_size = os.sysconf("SC_PAGE_SIZE")
        return rss_pages * page_size
    # conformance: ignore[E004] probe reads /proc/self/stat to detect Linux RSS; failure is expected on non-Linux
    except Exception:
        _logger.debug("Failed to read /proc/self/stat for RSS", exc_info=True)
        return None


def sample() -> ResourceSample | None:
    """Capture current process CPU time and memory.

    Returns:
        ResourceSample, or None if sampling fails (e.g. on Windows).
    """
    try:
        import resource  # noqa: PLC0415 — stdlib resource; not available on Windows (try/except wraps)
    except ImportError:  # conformance: ignore[E008] stdlib resource not available on Windows; None signals unavailable
        # resource module not available on Windows
        _logger.debug(
            "stdlib resource module unavailable; returning None", exc_info=True
        )
        return None

    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_time = usage.ru_utime + usage.ru_stime

        # Try /proc for accurate RSS on Linux
        rss = _read_proc_rss()
        if rss is None:
            # macOS: ru_maxrss is in bytes; Linux: in KB (but we use /proc above)
            rss = usage.ru_maxrss
            if sys.platform == "linux":
                rss *= 1024  # Linux reports KB

        return ResourceSample(cpu_time_s=cpu_time, rss_bytes=rss)
    except Exception:
        _logger.warning("Resource sampling failed; returning None", exc_info=True)
        return None


def compute_deltas(
    start: ResourceSample | None,
    end: ResourceSample | None,
    duration_s: float,
) -> tuple[float, float]:
    """Compute whole-process ``(cpu_seconds, mem_gb_sec)`` between two samples.

    ``cpu_seconds`` = CPU time the whole process consumed between the samples.
    ``mem_gb_sec`` = average RSS in GiB multiplied by wall-clock duration
    in seconds (unit: ``GiB.s``).

    This is the process's consumption over the window, not one activity's: with
    activities running concurrently it includes all of them. Use
    :class:`ActivityShareLedger` for per-activity attribution.

    Returns (0.0, 0.0) if either sample is None.
    """
    if start is None or end is None:
        return 0.0, 0.0

    cpu_seconds = max(0.0, end.cpu_time_s - start.cpu_time_s)
    mem_gb_sec = _mem_gib_seconds(start, end, duration_s)

    return cpu_seconds, mem_gb_sec


def _mem_gib_seconds(start: ResourceSample, end: ResourceSample, dt_s: float) -> float:
    """Trapezoid memory-time integral between two samples, in GiB.s."""
    avg_rss_gib = (start.rss_bytes + end.rss_bytes) / 2.0 / (1024**3)
    return avg_rss_gib * max(0.0, dt_s)


@dataclass(frozen=True)
class ShareMark:
    """An activity's position in the ledger's per-slot integrals at its start.

    ``sampled`` is False when the process could not be sampled at the start,
    in which case the activity still counts as in flight (it is consuming its
    share) but no value is reported for it.
    """

    cpu_per_slot_s: float
    mem_per_slot_gib_s: float
    sampled: bool


class ActivityShareLedger:
    """Fair-share attribution of process CPU and memory-time to activities.

    Keeps two running integrals — process CPU and process memory-time, each
    divided by the number of activities in flight. The in-flight count only
    changes at an activity boundary, and every boundary samples the process, so
    between two consecutive samples the divisor is constant and the division is
    exact. An activity's share is the integral at its end minus the integral at
    its start. Summed over activities this is the process's consumption during
    periods with at least one activity in flight: bounded by the process total,
    and equal to the old per-window value when activities do not overlap.

    Thread-safe: sync activities may finish on executor threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0
        self._last_sample: ResourceSample | None = None
        self._last_t_s = 0.0
        self._cpu_per_slot_s = 0.0
        self._mem_per_slot_gib_s = 0.0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def _advance(self, now: ResourceSample | None, now_s: float) -> None:
        """Fold the consumption since the previous boundary into the integrals.

        Caller holds the lock. A failed sample leaves the previous one in place,
        so the gap is attributed at the next good boundary instead of lost.
        """
        if now is None:
            return
        prev = self._last_sample
        if prev is not None and self._in_flight > 0:
            dt_s = now_s - self._last_t_s
            self._cpu_per_slot_s += (
                max(0.0, now.cpu_time_s - prev.cpu_time_s) / self._in_flight
            )
            self._mem_per_slot_gib_s += (
                _mem_gib_seconds(prev, now, dt_s) / self._in_flight
            )
        self._last_sample = now
        self._last_t_s = now_s

    def begin(self, now_s: float) -> ShareMark:
        """Register an activity starting at monotonic time ``now_s`` (seconds)."""
        with self._lock:
            now = sample()
            self._advance(now, now_s)
            self._in_flight += 1
            return ShareMark(
                cpu_per_slot_s=self._cpu_per_slot_s,
                mem_per_slot_gib_s=self._mem_per_slot_gib_s,
                sampled=now is not None,
            )

    def end(self, mark: ShareMark, now_s: float) -> tuple[float, float] | None:
        """Close the activity opened with ``mark``; return its ``(cpu_s, mem_gib_s)``.

        Returns ``None`` when either boundary could not be sampled. Must be
        called exactly once per :meth:`begin`, including on failure, or every
        later activity's share is diluted by a phantom slot.
        """
        with self._lock:
            now = sample()
            self._advance(now, now_s)
            self._in_flight = max(0, self._in_flight - 1)
            if not mark.sampled or now is None:
                return None
            return (
                max(0.0, self._cpu_per_slot_s - mark.cpu_per_slot_s),
                max(0.0, self._mem_per_slot_gib_s - mark.mem_per_slot_gib_s),
            )


ACTIVITY_SHARES = ActivityShareLedger()
"""The worker process's ledger. One per process: the samples are process-wide."""
