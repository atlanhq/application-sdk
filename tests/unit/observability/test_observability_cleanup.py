"""Unit tests for ``AtlanObservability._cleanup_old_records`` partition pruning.

Two properties are guarded here:

- Every partition ``rmtree`` is offloaded off the event loop. These trees hold a
  retention window's worth of gzipped records, and removing them inline stalls
  every other coroutine for the full duration of the call.
- Month- and day-level pruning actually runs. ``shutil`` used to be imported
  lazily *inside* the year-level branch, so any call that did not delete a whole
  year raised ``UnboundLocalError`` on the first month/day ``rmtree`` — swallowed
  whole by the method's broad ``except Exception``, leaving stale partitions on
  disk indefinitely.

No object store is touched: ``delete`` is mocked and ``ENABLE_ATLAN_UPLOAD`` is
left at its default so the upstream branch never runs.

The pruner reads wall-clock time to compute its retention cutoff, so these tests
pin the module's ``datetime`` to a fixed instant. Without that, which pruning
branch a given partition takes depends on the calendar day the suite runs.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from typing import Any
from unittest import mock

import pytest

from application_sdk.observability.observability import (
    LOCAL_OBS_SUBDIR_MAP,
    AtlanObservability,
)

# Mid-month, mid-year, so every partition below is unambiguously in the current
# year and month — the branch each test targets is fixed rather than dependent on
# the day the suite happens to run.
_FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is pinned to :data:`_FROZEN_NOW`.

    Patched over the *module's* ``datetime`` name only — a module-local clock
    seam, not a global clock patch, so the asyncio loop's own clock is untouched.
    """

    @classmethod
    def now(cls, tz=None) -> datetime:  # type: ignore[override]
        return _FROZEN_NOW.replace(tzinfo=tz) if tz else _FROZEN_NOW


def _frozen_clock():
    return mock.patch(
        "application_sdk.observability.observability.datetime", _FrozenDatetime
    )


class _StubObservability(AtlanObservability[dict[str, Any]]):
    """Minimal concrete subclass — the ABC's two exports are never exercised."""

    def process_record(self, record: dict[str, Any]) -> dict[str, Any]:
        return record

    def export_record(self, record: dict[str, Any]) -> None:  # pragma: no cover
        raise AssertionError("export_record must not run in these tests")


def _make_observability(tmp_path: Any, retention_days: int) -> _StubObservability:
    return _StubObservability(
        batch_size=100,
        flush_interval=60,
        retention_days=retention_days,
        cleanup_enabled=True,
        data_dir=str(tmp_path / "observability"),
        file_name="unused.parquet",
    )


def _partition_dir(obs: _StubObservability, when: datetime) -> str:
    """Build the on-disk partition directory the pruner walks for *when*."""
    signal_type = obs._get_signal_type()
    local_subdir = LOCAL_OBS_SUBDIR_MAP.get(signal_type, "non-sdr/other")
    path = os.path.join(
        obs.data_dir,
        local_subdir,
        f"year={when.year}",
        f"month={when.month}",
        f"day={when.day}",
    )
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "records.json.gz"), "wb") as handle:
        handle.write(b"\x00")
    return path


@pytest.fixture(autouse=True)
def _reset_instances():
    AtlanObservability._reset_for_testing()
    yield
    AtlanObservability._reset_for_testing()


class TestCleanupOldRecordsOffload:
    @pytest.mark.asyncio
    async def test_partition_removal_offloaded_to_thread(self, tmp_path) -> None:
        obs = _make_observability(tmp_path, retention_days=7)
        stale_day = _partition_dir(obs, _FROZEN_NOW - timedelta(days=30))

        with (
            _frozen_clock(),
            mock.patch("application_sdk.storage.delete", new_callable=mock.AsyncMock),
            mock.patch.object(
                AtlanObservability,
                "_get_deployment_store",
                return_value=mock.MagicMock(),
            ),
            mock.patch(
                "application_sdk._runtime.offload.run_in_thread",
                new_callable=mock.AsyncMock,
                side_effect=lambda func, *a, **kw: func(*a, **kw),
            ) as mock_offload,
        ):
            await obs._cleanup_old_records()

        assert mock_offload.await_args_list, "partition removal was not offloaded"
        assert all(
            call.args[0] is shutil.rmtree for call in mock_offload.await_args_list
        )
        assert not os.path.exists(stale_day)

    @pytest.mark.asyncio
    async def test_day_partition_pruned_without_year_deletion(self, tmp_path) -> None:
        """A stale *day* inside the current year must still be pruned.

        Regression guard: with ``shutil`` imported inside the year-level branch,
        this path raised ``UnboundLocalError`` and silently pruned nothing.

        The clock is frozen so both partitions land in the frozen year and month
        — only the day branch can prune them — and the guard therefore runs
        identically on every calendar day.
        """
        obs = _make_observability(tmp_path, retention_days=1)
        # Same year and month as the frozen now, so only the day branch applies.
        stale_day = _partition_dir(obs, _FROZEN_NOW.replace(day=1))
        fresh_day = _partition_dir(obs, _FROZEN_NOW)

        with (
            _frozen_clock(),
            mock.patch(
                "application_sdk.storage.delete", new_callable=mock.AsyncMock
            ) as mock_delete,
            mock.patch.object(
                AtlanObservability,
                "_get_deployment_store",
                return_value=mock.MagicMock(),
            ),
        ):
            await obs._cleanup_old_records()

        assert not os.path.exists(stale_day), "stale day partition was not pruned"
        assert os.path.exists(fresh_day), "fresh day partition must be retained"
        mock_delete.assert_awaited()
