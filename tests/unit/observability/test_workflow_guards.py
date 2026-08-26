"""Workflow-context guards across the observability package (SYSAPPS-328).

Three call sites must skip work Temporal's deterministic workflow loop cannot
support, and all three ask one shared predicate,
``observability.utils.in_temporal_workflow``:

* ``SegmentClient.flush()`` — the awaited cross-loop bridge that started this
  (``asyncio.wrap_future``).
* ``AtlanObservability._flush_records()`` — blocking gzip file I/O, the
  object-store upload, and the retention sweep's ``run_in_thread`` offload.
* ``AtlanLoggerAdapter._sync_flush()`` — reached on every ERROR/CRITICAL log,
  including the generated workflow's own "App failed"; would otherwise spawn an
  un-awaited task on the workflow loop, or build a second event loop.

Division of labour with the integration suite: the *mechanism* — why being in a
workflow breaks these operations at all — is pinned against a real Temporal
worker in ``tests/integration/test_segment_flush_workflow.py``, which fails if
the guard is removed. These unit tests pin the *wiring*: that each of the two
sibling call sites consults the predicate and honours it. Patching the predicate
is the subject of these tests, not a shortcut around them.

Every guard test is paired with a positive control asserting the same call does
perform its work when the predicate is False. Without that pair a guard test
cannot tell "the guard fired" from "the work never happened anyway", and would
keep passing if the guarded operation were later removed or renamed.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime
from typing import Any
from unittest import mock

import pytest

from application_sdk.constants import LOG_FILE_NAME
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter
from application_sdk.observability.observability import AtlanObservability
from application_sdk.observability.utils import in_temporal_workflow

_OBS_MODULE = "application_sdk.observability.observability"
_LOG_MODULE = "application_sdk.observability.logger_adaptor"

_BASE_TS = datetime(2026, 8, 26, 10, 30, 0).timestamp()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _ConcreteObs(AtlanObservability):
    """Minimal concrete subclass; the guard under test lives on the base."""

    def process_record(self, record: Any) -> dict[str, Any]:
        return record

    def export_record(self, record: Any) -> None:
        pass


@pytest.fixture
def obs_instance(tmp_path):
    AtlanObservability._reset_for_testing()
    instance = _ConcreteObs(
        batch_size=100,
        flush_interval=60,
        retention_days=7,
        cleanup_enabled=False,
        data_dir=str(tmp_path),
        file_name=LOG_FILE_NAME,  # → signal type "logs"
    )
    yield instance
    AtlanObservability._reset_for_testing()


@pytest.fixture
def logger_adapter():
    AtlanLoggerAdapter._reset_for_testing()
    with mock.patch.dict(
        "os.environ",
        {"LOG_LEVEL": "INFO", "ENABLE_OTLP_LOGS": "false"},
    ):
        yield AtlanLoggerAdapter("test_workflow_guards")
    AtlanLoggerAdapter._reset_for_testing()


def _record(ts: float) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "level": "INFO",
        "logger_name": "test",
        "message": "msg",
        "file": "test.py",
        "line": 1,
        "function": "test_fn",
        "extra": {},
    }


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


class TestInTemporalWorkflow:
    def test_false_outside_temporal(self):
        """The predicate must not fail closed.

        Guards built on it sit in front of the ordinary shutdown flush path
        (``main.py``'s SIGTERM handler, ``atexit``), so a predicate reading True
        here would silently disable observability everywhere.
        """
        assert in_temporal_workflow() is False

    def test_reads_the_temporal_runtime(self):
        """Ground truth is ``temporalio.workflow.in_workflow()``.

        Deliberately not the ``ExecutionContext`` ContextVar, which only
        populates after ``ExecutionContextInterceptor`` runs and would fail the
        guard *open* for a worker not built by ``create_worker``.
        """
        with mock.patch("temporalio.workflow.in_workflow", return_value=True):
            assert in_temporal_workflow() is True


# ---------------------------------------------------------------------------
# AtlanObservability._flush_records
# ---------------------------------------------------------------------------


class TestFlushRecordsWorkflowGuard:
    """The object-store sink is skipped in workflow context."""

    @staticmethod
    async def _flush_collecting_uploads(
        obs_instance, *, in_workflow: bool
    ) -> list[str]:
        """Run ``_flush_records`` with the predicate pinned; return upload keys."""
        uploads: list[str] = []

        async def _upload(key, local_path, store=None, **_kw):
            uploads.append(key)

        with ExitStack() as stack:
            stack.enter_context(
                mock.patch(f"{_OBS_MODULE}.ENABLE_OBSERVABILITY_STORE_SINK", True)
            )
            stack.enter_context(
                mock.patch(
                    f"{_OBS_MODULE}.in_temporal_workflow", return_value=in_workflow
                )
            )
            stack.enter_context(
                mock.patch("application_sdk.storage.upload_file", side_effect=_upload)
            )
            stack.enter_context(
                mock.patch.object(
                    obs_instance,
                    "_get_deployment_store",
                    return_value=mock.MagicMock(name="deployment_store"),
                )
            )
            await obs_instance._flush_records([_record(_BASE_TS)])

        return uploads

    @pytest.mark.asyncio
    async def test_skips_store_sink_in_workflow(self, obs_instance, tmp_path):
        """No gzip partition file is written and no upload is attempted."""
        uploads = await self._flush_collecting_uploads(obs_instance, in_workflow=True)

        assert uploads == []
        assert (
            list(tmp_path.rglob("*.json.gz")) == []
        ), "workflow-illegal file I/O ran despite the guard"

    @pytest.mark.asyncio
    async def test_writes_store_sink_outside_workflow(self, obs_instance, tmp_path):
        """Positive control: the same call flushes when not in a workflow.

        Without this, the test above would keep passing if the upload were
        removed from ``_flush_records`` entirely.
        """
        uploads = await self._flush_collecting_uploads(obs_instance, in_workflow=False)

        assert len(uploads) == 1, uploads


# ---------------------------------------------------------------------------
# AtlanLoggerAdapter._sync_flush
# ---------------------------------------------------------------------------


class TestSyncFlushWorkflowGuard:
    """Neither ``_sync_flush`` branch runs in workflow context."""

    @pytest.mark.asyncio
    async def test_schedules_no_task_in_workflow(self, logger_adapter):
        """Async branch: nothing is scheduled on the (workflow) running loop."""
        flushed: list[bool] = []

        async def _flush_buffer(force=False):
            flushed.append(force)

        with (
            mock.patch(f"{_LOG_MODULE}.in_temporal_workflow", return_value=True),
            mock.patch.object(logger_adapter, "_flush_buffer", _flush_buffer),
        ):
            before = len(asyncio.all_tasks())
            logger_adapter._sync_flush()
            after = len(asyncio.all_tasks())
            await asyncio.sleep(0)

        assert flushed == []
        assert after == before, "a task was scheduled on the workflow loop"

    @pytest.mark.asyncio
    async def test_schedules_task_outside_workflow(self, logger_adapter):
        """Positive control for the async branch."""
        flushed: list[bool] = []

        async def _flush_buffer(force=False):
            flushed.append(force)

        with (
            mock.patch(f"{_LOG_MODULE}.in_temporal_workflow", return_value=False),
            mock.patch.object(logger_adapter, "_flush_buffer", _flush_buffer),
        ):
            logger_adapter._sync_flush()
            await asyncio.sleep(0)

        assert flushed == [True]

    def test_builds_no_temporary_loop_in_workflow(self, logger_adapter):
        """Sync branch: no second event loop is constructed.

        Reached when ``get_running_loop()`` raises. Building a loop here is
        wrong in workflow context regardless of what it then ran, so assert on
        the construction rather than only on the flush.
        """
        with (
            mock.patch(f"{_LOG_MODULE}.in_temporal_workflow", return_value=True),
            mock.patch(f"{_LOG_MODULE}.asyncio.new_event_loop") as new_loop,
        ):
            logger_adapter._sync_flush()

        new_loop.assert_not_called()

    def test_builds_temporary_loop_outside_workflow(self, logger_adapter):
        """Positive control for the sync branch."""
        flushed: list[bool] = []

        async def _flush_buffer(force=False):
            flushed.append(force)

        with (
            mock.patch(f"{_LOG_MODULE}.in_temporal_workflow", return_value=False),
            mock.patch.object(logger_adapter, "_flush_buffer", _flush_buffer),
        ):
            logger_adapter._sync_flush()

        assert flushed == [True]
