"""Memory-pressure warning and startup baseline take the limit from the cgroup.

The container's enforced cgroup limit is read first, with K8S_POD_MEMORY_LIMIT
as the fallback, so the warning needs no Downward API wiring. While the ratio
stays at or above the threshold the warning repeats once per repeat interval.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import application_sdk.main as main_mod
from application_sdk.execution import heartbeat as hb_mod
from application_sdk.execution.heartbeat import auto_heartbeat_loop
from application_sdk.main import _log_process_memory_baseline
from application_sdk.observability.resource_sampler import ResourceSample

_GIB = 1024**3


def _pressure_warnings(mock_logger) -> list:
    return [
        c for c in mock_logger.warning.call_args_list if "Memory pressure" in str(c)
    ]


async def _run_ticks(ticks: int, limit: int | None, rss: int) -> list:
    seen = {"n": 0}
    stop = asyncio.Event()

    def hb_fn():
        seen["n"] += 1
        if seen["n"] >= ticks:
            stop.set()

    with (
        patch.object(hb_mod._cgroup, "memory_limit_bytes", return_value=limit),
        patch(
            "application_sdk.execution.heartbeat._resource_sampler.sample",
            return_value=ResourceSample(cpu_time_s=1.0, rss_bytes=rss),
        ),
        patch.object(hb_mod, "logger") as mock_logger,
    ):
        await auto_heartbeat_loop(0.001, hb_fn, stop, task_name="mem-task")
    return _pressure_warnings(mock_logger)


@pytest.mark.asyncio
async def test_warning_uses_cgroup_limit_without_env(monkeypatch) -> None:
    """A cgroup limit alone enables the warning; the env var is not needed."""
    monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)
    limit = 2 * _GIB
    stop = asyncio.Event()

    with (
        patch.object(hb_mod._cgroup, "_read_int", return_value=limit),
        patch(
            "application_sdk.execution.heartbeat._resource_sampler.sample",
            return_value=ResourceSample(cpu_time_s=1.0, rss_bytes=int(limit * 0.9)),
        ),
        patch.object(hb_mod, "logger") as mock_logger,
    ):
        await auto_heartbeat_loop(0.001, stop.set, stop, task_name="cg-task")

    warnings = _pressure_warnings(mock_logger)
    assert len(warnings) == 1
    _fmt, task, pct, _rss_gib, limit_gib = warnings[0].args
    assert task == "cg-task"
    assert abs(pct - 90.0) < 0.5
    assert limit_gib == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_silent_without_any_limit() -> None:
    """No limit from either source: no warning, however high RSS is."""
    assert await _run_ticks(ticks=2, limit=None, rss=10 * _GIB) == []


@pytest.mark.asyncio
async def test_warns_once_within_repeat_interval() -> None:
    """Several ticks above 80% inside one 5-minute interval: one warning."""
    limit = 4 * _GIB
    assert len(await _run_ticks(ticks=4, limit=limit, rss=int(limit * 0.85))) == 1


@pytest.mark.asyncio
async def test_repeats_each_interval_while_above_threshold() -> None:
    """With the interval forced to 0 every tick qualifies, so every tick warns."""
    limit = 4 * _GIB
    with patch.object(hb_mod, "_MEMORY_WARN_REPEAT_SECONDS", 0.0):
        warnings = await _run_ticks(ticks=3, limit=limit, rss=int(limit * 0.85))
    assert len(warnings) == 3


def test_startup_baseline_uses_cgroup_limit(monkeypatch) -> None:
    """The startup INFO line reads the cgroup limit when the env var is unset."""
    monkeypatch.delenv("K8S_POD_MEMORY_LIMIT", raising=False)
    with (
        patch(
            "application_sdk.observability.cgroup.memory_limit_bytes",
            return_value=8 * _GIB,
        ),
        patch(
            "application_sdk.observability.resource_sampler.sample",
            return_value=ResourceSample(cpu_time_s=0.5, rss_bytes=2 * _GIB),
        ),
        patch.object(main_mod, "logger") as mock_logger,
    ):
        _log_process_memory_baseline()

    info = [
        c
        for c in mock_logger.info.call_args_list
        if "memory at start" in str(c).lower()
    ]
    assert info
    _fmt, _rss, _lim, pct = info[0].args
    assert abs(pct - 25.0) < 0.5


def test_startup_baseline_silent_without_any_limit() -> None:
    with (
        patch(
            "application_sdk.observability.cgroup.memory_limit_bytes",
            return_value=None,
        ),
        patch.object(main_mod, "logger") as mock_logger,
    ):
        _log_process_memory_baseline()
    mock_logger.info.assert_not_called()
