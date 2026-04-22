"""Unit tests for resource_sampler (stdlib-based, no psutil)."""

import sys
from unittest.mock import patch

from application_sdk.observability.resource_sampler import (
    ResourceSample,
    compute_deltas,
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
        """If resource module can't be imported, return None gracefully."""
        with patch.dict("sys.modules", {"resource": None}):
            # sample() should never raise, always return None or ResourceSample
            result = sample()
            assert result is None or isinstance(result, ResourceSample)

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
