"""Tests for cgroup sizing telemetry. Builds a real fake hierarchy on disk, not an
``open`` mock: the quirks that matter are file-shaped.
"""

import asyncio
import os

import pytest

from application_sdk.observability import cgroup


def _write(tmp_path, name: str, contents: str) -> str:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return str(path)


def _missing(tmp_path) -> tuple[str, ...]:
    return (str(tmp_path / "does-not-exist"),)


class TestMemoryUsage:
    def test_reads_v2(self, tmp_path, monkeypatch):
        p = _write(tmp_path, "memory.current", "4096\n")
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", (p,))
        assert cgroup.memory_usage_bytes() == 4096

    def test_falls_back_to_v1(self, tmp_path, monkeypatch):
        v1 = _write(tmp_path, "usage_in_bytes", "8192")
        monkeypatch.setattr(
            cgroup, "_MEMORY_CURRENT_PATHS", (str(tmp_path / "nope"), v1)
        )
        assert cgroup.memory_usage_bytes() == 8192

    def test_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        assert cgroup.memory_usage_bytes() is None

    def test_malformed_file_falls_through_to_the_next_path(self, tmp_path, monkeypatch):
        """A garbage v2 file must not shadow a readable v1 one."""
        bad = _write(tmp_path, "memory.current", "not-a-number")
        good = _write(tmp_path, "usage_in_bytes", "777")
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", (bad, good))
        assert cgroup.memory_usage_bytes() == 777


class TestMemoryLimit:
    def test_cgroup_wins_over_env(self, tmp_path, monkeypatch):
        """The enforced limit beats the requested one — they diverge under a VPA."""
        p = _write(tmp_path, "memory.max", str(2 * 1024**3))
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", (p,))
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", "16Gi")
        assert cgroup.memory_limit_bytes() == 2 * 1024**3

    def test_v2_literal_max_falls_back_to_env(self, tmp_path, monkeypatch):
        p = _write(tmp_path, "memory.max", "max\n")
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", (p,))
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", "4Gi")
        assert cgroup.memory_limit_bytes() == 4 * 1024**3

    def test_v1_unlimited_sentinel_falls_back_to_env(self, tmp_path, monkeypatch):
        """Taken literally, v1's sentinel makes every activity look tiny."""
        p = _write(tmp_path, "limit_in_bytes", str(9223372036854771712))
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", (p,))
        monkeypatch.setenv("K8S_POD_MEMORY_LIMIT", "1Gi")
        assert cgroup.memory_limit_bytes() == 1024**3

    def test_none_when_unlimited_and_no_env(self, tmp_path, monkeypatch):
        p = _write(tmp_path, "memory.max", "max")
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", (p,))
        monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)
        assert cgroup.memory_limit_bytes() is None

    def test_fraction(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "512"),),
        )
        monkeypatch.setattr(
            cgroup, "_MEMORY_LIMIT_PATHS", (_write(tmp_path, "memory.max", "2048"),)
        )
        assert cgroup.memory_fraction() == 0.25

    def test_fraction_is_none_without_a_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "512"),),
        )
        monkeypatch.setattr(cgroup, "_MEMORY_LIMIT_PATHS", _missing(tmp_path))
        monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)
        assert cgroup.memory_fraction() is None


class TestResetMemoryPeak:
    def test_proven_reset(self, tmp_path, monkeypatch):
        peak = _write(tmp_path, "memory.peak", "5000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (peak,))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        assert cgroup.reset_memory_peak() is True
        assert cgroup.memory_peak_bytes() == 0

    def test_unprovable_when_watermark_equals_current_usage(
        self, tmp_path, monkeypatch
    ):
        """No headroom to observe a drop means the reset cannot be proven. Guessing
        True would report the pod's lifetime watermark as this peak.
        """
        monkeypatch.setattr(
            cgroup, "_MEMORY_PEAK_PATHS", (_write(tmp_path, "memory.peak", "1000"),)
        )
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        assert cgroup.reset_memory_peak() is False

    def test_read_only_peak_is_not_proven(self, tmp_path, monkeypatch):
        """``memory.peak`` is read-only before Linux 6.8 — the common case."""
        peak = tmp_path / "memory.peak"
        peak.write_text("5000")
        os.chmod(peak, 0o444)
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (str(peak),))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        assert cgroup.reset_memory_peak() is False

    def test_false_when_no_cgroup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        assert cgroup.reset_memory_peak() is False


class TestPrepareMemoryWatermark:
    def test_usable_without_a_write_when_nothing_is_stale(self, tmp_path, monkeypatch):
        """Watermark already at current usage: no earlier peak to clear. This is the
        case that made the watermark path unreachable in practice.
        """
        peak = tmp_path / "memory.peak"
        peak.write_text("1000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (str(peak),))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        assert cgroup.prepare_memory_watermark() is True
        # Left alone: there was nothing to clear, so no write was needed.
        assert peak.read_text() == "1000"

    def test_resets_when_the_watermark_carries_an_earlier_peak(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            cgroup, "_MEMORY_PEAK_PATHS", (_write(tmp_path, "memory.peak", "5000"),)
        )
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        assert cgroup.prepare_memory_watermark() is True
        assert cgroup.memory_peak_bytes() == 0

    def test_false_when_a_stale_watermark_cannot_be_cleared(
        self, tmp_path, monkeypatch
    ):
        """Read-only ``memory.peak`` (pre-6.8) with a stale peak — must poll."""
        peak = tmp_path / "memory.peak"
        peak.write_text("5000")
        os.chmod(peak, 0o444)
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (str(peak),))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        assert cgroup.prepare_memory_watermark() is False

    def test_false_when_no_cgroup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        assert cgroup.prepare_memory_watermark() is False


class TestPeakDelta:
    def test_delta_excludes_memory_resident_at_entry(self):
        trace = cgroup.ContainerTrace(peak_memory_bytes=3000, start_memory_bytes=1000)
        assert trace.peak_delta_bytes == 2000

    def test_delta_is_none_without_a_baseline(self):
        """No baseline means the bias cannot be removed — not that it is zero."""
        assert cgroup.ContainerTrace(peak_memory_bytes=3000).peak_delta_bytes is None
        assert cgroup.ContainerTrace(start_memory_bytes=1000).peak_delta_bytes is None

    def test_delta_floors_at_zero(self):
        """Memory returned to the kernel mid-block can leave peak below start."""
        trace = cgroup.ContainerTrace(peak_memory_bytes=800, start_memory_bytes=1000)
        assert trace.peak_delta_bytes == 0

    def test_two_pods_with_different_history_agree_on_the_delta(self):
        """The point of the field."""
        cold = cgroup.ContainerTrace(peak_memory_bytes=2_500, start_memory_bytes=400)
        warm = cgroup.ContainerTrace(peak_memory_bytes=3_600, start_memory_bytes=1_500)
        assert cold.peak_memory_bytes != warm.peak_memory_bytes
        assert cold.peak_delta_bytes == warm.peak_delta_bytes == 2_100


class TestCpuStat:
    def test_v2(self, tmp_path, monkeypatch):
        p = _write(
            tmp_path,
            "cpu.stat",
            "usage_usec 2500000\nuser_usec 2000000\nsystem_usec 500000\n"
            "nr_periods 100\nnr_throttled 30\nthrottled_usec 750000\n",
        )
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", p)
        stat = cgroup.cpu_stat()
        assert stat is not None
        assert stat.usage_seconds == pytest.approx(2.5)
        assert stat.nr_periods == 100
        assert stat.nr_throttled == 30
        assert stat.throttled_seconds == pytest.approx(0.75)

    def test_v1_uses_nanoseconds(self, tmp_path, monkeypatch):
        """v1 counts ns; the v2 divisor would understate throttling 1000x."""
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", str(tmp_path / "nope"))
        monkeypatch.setattr(
            cgroup,
            "_CPU_STAT_V1",
            _write(
                tmp_path,
                "v1/cpu.stat",
                "nr_periods 200\nnr_throttled 40\nthrottled_time 1500000000\n",
            ),
        )
        monkeypatch.setattr(
            cgroup,
            "_CPUACCT_USAGE_V1",
            _write(tmp_path, "v1/cpuacct.usage", "3000000000"),
        )
        stat = cgroup.cpu_stat()
        assert stat is not None
        assert stat.usage_seconds == pytest.approx(3.0)
        assert stat.throttled_seconds == pytest.approx(1.5)
        assert stat.nr_throttled == 40

    def test_v1_without_cpuacct_still_returns_throttling(self, tmp_path, monkeypatch):
        """Throttling is the sizing signal; don't lose it to a missing mount."""
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", str(tmp_path / "nope"))
        monkeypatch.setattr(
            cgroup,
            "_CPU_STAT_V1",
            _write(tmp_path, "v1/cpu.stat", "nr_periods 10\nnr_throttled 5\n"),
        )
        monkeypatch.setattr(cgroup, "_CPUACCT_USAGE_V1", str(tmp_path / "nope"))
        stat = cgroup.cpu_stat()
        assert stat is not None
        assert stat.usage_seconds == 0.0
        assert stat.nr_throttled == 5

    def test_none_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", str(tmp_path / "nope"))
        monkeypatch.setattr(cgroup, "_CPU_STAT_V1", str(tmp_path / "nope-either"))
        assert cgroup.cpu_stat() is None


class TestCpuQuota:
    def test_v2(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            cgroup, "_CPU_MAX_V2", _write(tmp_path, "cpu.max", "150000 100000\n")
        )
        assert cgroup.cpu_quota_cores() == pytest.approx(1.5)

    def test_v2_max_falls_back_to_v1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            cgroup, "_CPU_MAX_V2", _write(tmp_path, "cpu.max", "max 100000")
        )
        monkeypatch.setattr(
            cgroup, "_CPU_QUOTA_V1", _write(tmp_path, "quota", "200000")
        )
        monkeypatch.setattr(
            cgroup, "_CPU_PERIOD_V1", _write(tmp_path, "period", "100000")
        )
        assert cgroup.cpu_quota_cores() == pytest.approx(2.0)

    def test_v1_unlimited_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cgroup, "_CPU_MAX_V2", str(tmp_path / "nope"))
        monkeypatch.setattr(cgroup, "_CPU_QUOTA_V1", _write(tmp_path, "quota", "-1"))
        monkeypatch.setattr(
            cgroup, "_CPU_PERIOD_V1", _write(tmp_path, "period", "100000")
        )
        assert cgroup.cpu_quota_cores() is None


class TestContainerTrace:
    def test_observe_keeps_the_maximum(self):
        trace = cgroup.ContainerTrace(memory_limit_bytes=1000)
        trace.observe(400, 1000)
        trace.observe(900, 1000)
        trace.observe(600, 1000)
        assert trace.peak_memory_bytes == 900
        assert trace.peak_memory_fraction == pytest.approx(0.9)

    def test_observe_ignores_none(self):
        trace = cgroup.ContainerTrace(memory_limit_bytes=1000)
        trace.observe(400, 1000)
        trace.observe(None, 1000)
        assert trace.peak_memory_bytes == 400

    def test_throttled_fraction(self):
        trace = cgroup.ContainerTrace(cpu_periods=200, cpu_throttled_periods=50)
        assert trace.throttled_fraction == pytest.approx(0.25)

    def test_throttled_fraction_none_without_periods(self):
        """Zero periods means too short to schedule, not 0% throttled."""
        assert cgroup.ContainerTrace(cpu_periods=0).throttled_fraction is None
        assert cgroup.ContainerTrace().throttled_fraction is None


class TestTrackContainerUsage:
    @pytest.mark.asyncio
    async def test_watermark_mode_catches_a_spike_that_ended(
        self, tmp_path, monkeypatch
    ):
        """A spike already freed by the time the block exits. An endpoint-only sampler
        would read 1000 and size the tier 10x too small.
        """
        peak = tmp_path / "memory.peak"
        peak.write_text("5000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (str(peak),))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        monkeypatch.setattr(
            cgroup, "_MEMORY_LIMIT_PATHS", (_write(tmp_path, "memory.max", "10000"),)
        )

        async with cgroup.track_container_usage() as trace:
            peak.write_text("9000")  # the kernel observing the spike

        assert trace.peak_source == "watermark"
        assert trace.peak_memory_bytes == 9000
        assert trace.peak_memory_fraction == pytest.approx(0.9)
        # Captured in watermark mode too: resetting the counter does not free the
        # pages, so the watermark still starts from whatever was resident.
        assert trace.start_memory_bytes == 1000
        assert trace.peak_delta_bytes == 8000

    @pytest.mark.asyncio
    async def test_watermark_mode_on_a_pod_with_no_earlier_peak(
        self, tmp_path, monkeypatch
    ):
        """peak == current at entry: the shape of a pod's first activity."""
        peak = tmp_path / "memory.peak"
        peak.write_text("1000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", (str(peak),))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        monkeypatch.setattr(
            cgroup, "_MEMORY_LIMIT_PATHS", (_write(tmp_path, "memory.max", "10000"),)
        )

        async with cgroup.track_container_usage(poll_interval_seconds=60) as trace:
            peak.write_text("7000")  # a spike far shorter than the poll interval

        assert trace.peak_source == "watermark"
        assert trace.peak_memory_bytes == 7000
        assert trace.start_memory_bytes == 1000
        assert trace.peak_delta_bytes == 6000

    @pytest.mark.asyncio
    async def test_poll_mode_still_records_the_baseline(self, tmp_path, monkeypatch):
        """The delta has to survive the fallback, or it is absent when most needed."""
        current = tmp_path / "memory.current"
        current.write_text("1200")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", (str(current),))
        monkeypatch.setattr(
            cgroup, "_MEMORY_LIMIT_PATHS", (_write(tmp_path, "memory.max", "10000"),)
        )

        async with cgroup.track_container_usage(poll_interval_seconds=0.01) as trace:
            current.write_text("4200")
            await asyncio.sleep(0.05)

        assert trace.peak_source == "poll"
        assert trace.start_memory_bytes == 1200
        assert trace.peak_memory_bytes == 4200
        assert trace.peak_delta_bytes == 3000

    @pytest.mark.asyncio
    async def test_poll_mode_when_the_watermark_is_unavailable(
        self, tmp_path, monkeypatch
    ):
        current = tmp_path / "memory.current"
        current.write_text("1000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", (str(current),))
        monkeypatch.setattr(
            cgroup, "_MEMORY_LIMIT_PATHS", (_write(tmp_path, "memory.max", "10000"),)
        )

        async with cgroup.track_container_usage(poll_interval_seconds=0.01) as trace:
            current.write_text("7000")
            await asyncio.sleep(0.05)
            current.write_text("2000")
            await asyncio.sleep(0.05)

        assert trace.peak_source == "poll"
        # The spike is kept after usage drops back — that is the whole point of
        # polling for a peak rather than reading the endpoint.
        assert trace.peak_memory_bytes == 7000
        assert trace.peak_memory_fraction == pytest.approx(0.7)

    @pytest.mark.asyncio
    async def test_zero_interval_disables_polling(self, tmp_path, monkeypatch):
        """The fleet-wide default has to be able to cost nothing."""
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        async with cgroup.track_container_usage(poll_interval_seconds=0) as trace:
            pass
        assert trace.peak_source == "unavailable"
        assert trace.peak_memory_bytes is None

    @pytest.mark.asyncio
    async def test_unavailable_when_there_is_no_cgroup(self, tmp_path, monkeypatch):
        """macOS and local dev. Must be None, never 0."""
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", str(tmp_path / "nope"))
        monkeypatch.setattr(cgroup, "_CPU_STAT_V1", str(tmp_path / "nope"))

        async with cgroup.track_container_usage() as trace:
            pass

        assert trace.peak_source == "unavailable"
        assert trace.peak_memory_bytes is None
        assert trace.cpu_seconds is None

    @pytest.mark.asyncio
    async def test_cpu_counters_are_differenced(self, tmp_path, monkeypatch):
        stat = tmp_path / "cpu.stat"
        stat.write_text(
            "usage_usec 1000000\nnr_periods 100\nnr_throttled 10\n"
            "throttled_usec 500000\n"
        )
        monkeypatch.setattr(cgroup, "_CPU_STAT_V2", str(stat))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))

        async with cgroup.track_container_usage() as trace:
            stat.write_text(
                "usage_usec 4000000\nnr_periods 400\nnr_throttled 100\n"
                "throttled_usec 2500000\n"
            )

        assert trace.cpu_seconds == pytest.approx(3.0)
        assert trace.cpu_throttled_seconds == pytest.approx(2.0)
        assert trace.cpu_throttled_periods == 90
        assert trace.cpu_periods == 300
        assert trace.throttled_fraction == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_never_fails_a_successful_block(self, tmp_path, monkeypatch):
        """An instrument must not fail the thing it measures."""
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(
            cgroup, "cpu_stat", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        async with cgroup.track_container_usage() as trace:
            result = "done"

        assert result == "done"
        assert trace.cpu_seconds is None

    @pytest.mark.asyncio
    async def test_a_poller_that_raises_does_not_fail_the_block(
        self, tmp_path, monkeypatch
    ):
        """``await task`` on exit re-raises what the poller hit; that must cost
        telemetry, not the activity's result."""
        current = tmp_path / "memory.current"
        current.write_text("1000")
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", (str(current),))

        calls = {"n": 0}
        real = cgroup.memory_usage_bytes

        def flaky():
            calls["n"] += 1
            if calls["n"] > 1:  # the first call is the setup availability probe
                raise RuntimeError("cgroup went away")
            return real()

        monkeypatch.setattr(cgroup, "memory_usage_bytes", flaky)

        async with cgroup.track_container_usage(poll_interval_seconds=0.01) as trace:
            await asyncio.sleep(0.05)
            result = "done"

        assert result == "done"
        assert trace.peak_source == "poll"

    @pytest.mark.asyncio
    async def test_propagates_the_blocks_own_error(self, tmp_path, monkeypatch):
        """Telemetry must not swallow the activity's failure either."""
        monkeypatch.setattr(cgroup, "_MEMORY_CURRENT_PATHS", _missing(tmp_path))
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))

        with pytest.raises(ValueError, match="activity failed"):
            async with cgroup.track_container_usage():
                raise ValueError("activity failed")

    @pytest.mark.asyncio
    async def test_poll_task_is_cancelled_on_exit(self, tmp_path, monkeypatch):
        """A leaked poller per activity accumulates for the pod's lifetime."""
        monkeypatch.setattr(cgroup, "_MEMORY_PEAK_PATHS", _missing(tmp_path))
        monkeypatch.setattr(
            cgroup,
            "_MEMORY_CURRENT_PATHS",
            (_write(tmp_path, "memory.current", "1000"),),
        )
        before = len(asyncio.all_tasks())
        async with cgroup.track_container_usage(poll_interval_seconds=0.01):
            await asyncio.sleep(0.02)
        assert len(asyncio.all_tasks()) <= before
