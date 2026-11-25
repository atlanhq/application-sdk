"""``get_actual_cpu_count`` reads the container's CPU limit, not just the node's cores."""

import pytest

from application_sdk._runtime import cgroup
from application_sdk.common import concurrency
from application_sdk.common.concurrency import (
    get_actual_cpu_count,
    get_safe_num_threads,
)


def _limit_to(monkeypatch, tmp_path, cpu_max: str, node_cores: int = 16) -> None:
    """A real cgroup v2 ``cpu.max`` on disk, and a node with ``node_cores`` cores."""
    path = tmp_path / "cpu.max"
    path.write_text(cpu_max)
    monkeypatch.setattr(cgroup, "CPU_MAX_V2", str(path))
    monkeypatch.setattr(cgroup, "CPU_QUOTA_V1", str(tmp_path / "missing"))
    monkeypatch.setattr(cgroup, "CPU_PERIOD_V1", str(tmp_path / "missing"))
    monkeypatch.setattr(cgroup, "_affinity_count", lambda: node_cores)


def test_one_cpu_quota_on_a_16_core_node_is_one(monkeypatch, tmp_path):
    """The case the old affinity-only reader got wrong: it returned 16 here."""
    _limit_to(monkeypatch, tmp_path, "100000 100000")
    assert get_actual_cpu_count() == 1


def test_fractional_quota_rounds_down(monkeypatch, tmp_path):
    _limit_to(monkeypatch, tmp_path, "250000 100000")
    assert get_actual_cpu_count() == 2


def test_no_quota_falls_back_to_affinity(monkeypatch, tmp_path):
    _limit_to(monkeypatch, tmp_path, "max 100000", node_cores=6)
    assert get_actual_cpu_count() == 6


def test_agrees_with_the_runtime_reader(monkeypatch, tmp_path):
    """One answer for the CPU limit across the SDK's pools and the public helper."""
    _limit_to(monkeypatch, tmp_path, "300000 100000")
    assert get_actual_cpu_count() == cgroup.available_cpus()


def test_get_actual_cpu_count_does_not_warn(recwarn):
    get_actual_cpu_count()
    assert not [w for w in recwarn if w.category is DeprecationWarning]


def test_get_safe_num_threads_is_twice_the_limit_and_warns(monkeypatch, tmp_path):
    _limit_to(monkeypatch, tmp_path, "100000 100000")
    with pytest.warns(DeprecationWarning, match="removed in v4.0"):
        assert get_safe_num_threads() == 2


def test_run_in_thread_still_reexported():
    from application_sdk._runtime.offload import run_in_thread

    assert concurrency.run_in_thread is run_in_thread
