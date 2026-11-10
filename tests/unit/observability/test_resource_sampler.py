"""Unit tests for resource_sampler (stdlib-based, no psutil)."""

import sys
from unittest.mock import patch

import pytest

from application_sdk.observability.resource_sampler import (
    ActivityShareLedger,
    ResourceSample,
    compute_deltas,
    parse_pod_memory_limit,
    sample,
)


class TestSample:
    def test_sample_returns_resource_sample_on_unix(self):
        """On Mac/Linux, sample() should return a ResourceSample."""
        if sys.platform == "win32":
            # resource module unavailable on Windows
            assert sample() is None
            return

        result = sample()
        assert result is not None
        assert isinstance(result, ResourceSample)
        assert result.cpu_time_s >= 0
        assert result.rss_bytes > 0

    def test_sample_returns_none_when_resource_unavailable(self):
        """If resource module can't be imported, return None."""
        with patch.dict("sys.modules", {"resource": None}):
            result = sample()
            assert result is None

    def test_sample_handles_exception_gracefully(self):
        """If getrusage raises, return None."""
        if sys.platform == "win32":
            return

        with patch("resource.getrusage", side_effect=OSError("mocked")):
            result = sample()
            assert result is None


class TestComputeDeltas:
    def test_both_none_returns_zeros(self):
        assert compute_deltas(None, None, 10.0) == (0.0, 0.0)

    def test_start_none_returns_zeros(self):
        end = ResourceSample(cpu_time_s=5.0, rss_bytes=1024 * 1024 * 512)
        assert compute_deltas(None, end, 10.0) == (0.0, 0.0)

    def test_end_none_returns_zeros(self):
        start = ResourceSample(cpu_time_s=1.0, rss_bytes=1024 * 1024 * 256)
        assert compute_deltas(start, None, 10.0) == (0.0, 0.0)

    def test_normal_deltas(self):
        start = ResourceSample(cpu_time_s=1.0, rss_bytes=1024**3)  # 1 GiB
        end = ResourceSample(cpu_time_s=3.0, rss_bytes=1024**3)  # 1 GiB
        cpu, mem = compute_deltas(start, end, 10.0)
        assert cpu == 2.0
        assert mem == 10.0  # 1 GiB * 10s = 10 GiB.s

    def test_zero_duration(self):
        start = ResourceSample(cpu_time_s=1.0, rss_bytes=1024**3)
        end = ResourceSample(cpu_time_s=2.0, rss_bytes=1024**3)
        cpu, mem = compute_deltas(start, end, 0.0)
        assert cpu == 1.0
        assert mem == 0.0

    def test_negative_cpu_clamped_to_zero(self):
        """CPU time should never go backwards, but clamp to 0 if it does."""
        start = ResourceSample(cpu_time_s=5.0, rss_bytes=1024**3)
        end = ResourceSample(cpu_time_s=3.0, rss_bytes=1024**3)
        cpu, mem = compute_deltas(start, end, 10.0)
        assert cpu == 0.0


# ---------------------------------------------------------------------------
# parse_pod_memory_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Binary SI suffixes
        ("1Ki", 1024),
        ("2Mi", 2 * 1024**2),
        ("3Gi", 3 * 1024**3),
        ("4Ti", 4 * 1024**4),
        ("5Pi", 5 * 1024**5),
        ("6Ei", 6 * 1024**6),
        # Decimal SI suffixes
        ("1k", 1000),
        ("2M", 2 * 1000**2),
        ("3G", 3 * 1000**3),
        ("4T", 4 * 1000**4),
        ("5P", 5 * 1000**5),
        ("6E", 6 * 1000**6),
        # Raw bytes (resourceFieldRef divisor="1")
        ("0", 0),
        ("1024", 1024),
        ("4294967296", 4 * 1024**3),
        # Leading/trailing whitespace is stripped
        ("  2Gi  ", 2 * 1024**3),
        # Empty / whitespace-only → 0
        ("", 0),
        ("   ", 0),
        # Suffix only (no numeric part) → 0
        ("Gi", 0),
        ("Mi", 0),
        # Invalid / garbage → 0
        ("notanumber", 0),
        ("12.5Gi", 0),  # float not accepted
    ],
)
def test_parse_pod_memory_limit(raw: str, expected: int) -> None:
    assert parse_pod_memory_limit(raw) == expected


class _FakeProcess:
    """A process whose CPU and RSS the test sets; stands in for ``sample()``."""

    def __init__(self, rss_bytes: int = 1024**3) -> None:
        self.cpu_time_s = 0.0
        self.rss_bytes = rss_bytes
        self.fail = False

    def sample(self) -> ResourceSample | None:
        if self.fail:
            return None
        return ResourceSample(cpu_time_s=self.cpu_time_s, rss_bytes=self.rss_bytes)


@pytest.fixture
def proc():
    fake = _FakeProcess()
    with patch(
        "application_sdk.observability.resource_sampler.sample", side_effect=fake.sample
    ):
        yield fake


class TestActivityShareLedger:
    def test_single_activity_matches_window_delta(self, proc):
        """No overlap: the share is the whole-process window delta, unchanged."""
        ledger = ActivityShareLedger()
        mark = ledger.begin(0.0)
        proc.cpu_time_s = 0.5
        assert ledger.end(mark, 2.0) == pytest.approx((0.5, 2.0))
        assert ledger.in_flight == 0

    def test_overlapping_activities_sum_to_process_total(self, proc):
        """The reported repro: 4 overlapping activities, 0.59 s of process CPU.

        Per-window deltas summed to 2.12 s (3.6x); shares must sum to 0.59 s.
        """
        ledger = ActivityShareLedger()
        marks = []
        for i in range(4):
            proc.cpu_time_s = 0.01 * i
            marks.append(ledger.begin(float(i)))
        results = []
        for i, mark in enumerate(marks):
            proc.cpu_time_s = 0.50 + 0.03 * i
            results.append(ledger.end(mark, 10.0 + i))
        process_cpu = proc.cpu_time_s  # started at 0.0

        cpu_sum = sum(r[0] for r in results)
        mem_sum = sum(r[1] for r in results)
        assert cpu_sum == pytest.approx(process_cpu)
        # 1 GiB resident across the busy span t=0..13.
        assert mem_sum == pytest.approx(13.0)
        # The old attribution, for contrast: each window charged the whole process.
        windowed = sum((0.50 + 0.03 * i) - 0.01 * i for i in range(4))
        assert windowed > 3 * process_cpu

    def test_concurrent_shares_split_evenly(self, proc):
        """Two activities fully overlapping one CPU second get half each."""
        ledger = ActivityShareLedger()
        a = ledger.begin(0.0)
        b = ledger.begin(0.0)
        proc.cpu_time_s = 1.0
        assert ledger.end(a, 1.0) == pytest.approx((0.5, 0.5))
        assert ledger.end(b, 1.0) == pytest.approx((0.5, 0.5))

    def test_idle_time_is_not_attributed(self, proc):
        """CPU burnt while nothing is in flight belongs to no activity."""
        ledger = ActivityShareLedger()
        ledger.end(ledger.begin(0.0), 1.0)
        proc.cpu_time_s = 5.0  # idle-time CPU (polling, GC)
        mark = ledger.begin(10.0)
        proc.cpu_time_s = 5.25
        assert ledger.end(mark, 11.0) == pytest.approx((0.25, 1.0))

    def test_failed_start_sample_reports_nothing_but_holds_a_slot(self, proc):
        ledger = ActivityShareLedger()
        proc.fail = True
        blind = ledger.begin(0.0)
        proc.fail = False
        seen = ledger.begin(0.0)
        proc.cpu_time_s = 1.0
        assert ledger.end(blind, 1.0) is None
        # The blind activity still took its half of the overlap.
        assert ledger.end(seen, 1.0) == pytest.approx((0.5, 0.5))
        assert ledger.in_flight == 0

    def test_failed_end_sample_reports_nothing_and_releases_slot(self, proc):
        ledger = ActivityShareLedger()
        mark = ledger.begin(0.0)
        proc.fail = True
        assert ledger.end(mark, 1.0) is None
        assert ledger.in_flight == 0
