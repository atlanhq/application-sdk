import io
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pyarrow.parquet as pq

from application_sdk.lakehouse.events_ack import _ack_path, _build_ack_arrow, events_ack
from application_sdk.lakehouse.models import EventResult


class TestAckPath(unittest.TestCase):
    def test_path_structure(self):
        ts = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
        path = _ack_path(
            "databricks", "reverse-sync", "run-123", "events_ack.parquet", now=ts
        )
        self.assertEqual(
            path,
            "artifacts/databricks/reverse-sync/2026/04/30/run-123/events_ack.parquet",
        )

    def test_path_zero_pads_month_and_day(self):
        ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
        path = _ack_path("a", "w", "r", "f.parquet", now=ts)
        self.assertIn("/2026/01/05/", path)


class TestEventsAckBuildArrow(unittest.TestCase):
    """Internal: ack-arrow construction. ``_build_ack_arrow`` is private."""

    def test_aligns_events_and_results(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        results = [
            EventResult(status="SUCCESS"),
            EventResult(status="FAILED", error_message="boom"),
        ]
        arrow = _build_ack_arrow(events, results)
        self.assertEqual(arrow.num_rows, 2)
        self.assertEqual(arrow["event_id"].to_pylist(), ["e1", "e2"])
        self.assertEqual(arrow["status"].to_pylist(), ["SUCCESS", "FAILED"])
        self.assertEqual(arrow["error_message"].to_pylist(), [None, "boom"])

    def test_raises_on_length_mismatch(self):
        with self.assertRaises(ValueError):
            _build_ack_arrow([{"event_id": "e1"}], [EventResult(status="SUCCESS")] * 2)


class TestEventsAck(unittest.IsolatedAsyncioTestCase):
    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_uploads_parquet(self, mock_upload):
        events = [{"event_id": "e1"}]
        results = [EventResult(status="SUCCESS")]

        path = await events_ack(
            events,
            results,
            app_name="databricks",
            workflow_name="reverse-sync",
            workflow_run_id="run-123",
        )

        self.assertTrue(path.startswith("artifacts/databricks/reverse-sync/"))
        self.assertTrue(path.endswith("/run-123/events_ack.parquet"))
        mock_upload.assert_awaited_once()
        kwargs = mock_upload.await_args.kwargs
        self.assertEqual(kwargs["key"], path)
        # round-trip the parquet bytes to verify the schema
        arrow_back = pq.read_table(io.BytesIO(kwargs["content"]))
        self.assertEqual(arrow_back["event_id"].to_pylist(), ["e1"])
        self.assertEqual(arrow_back["status"].to_pylist(), ["SUCCESS"])

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_custom_filename(self, mock_upload):
        await events_ack(
            [{"event_id": "e1"}],
            [EventResult(status="SUCCESS")],
            app_name="a",
            workflow_name="w",
            workflow_run_id="r1",
            filename="ack.parquet",
        )
        kwargs = mock_upload.await_args.kwargs
        self.assertTrue(kwargs["key"].endswith("/r1/ack.parquet"))

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_uses_arrow_schema_with_nullable_error_message(self, mock_upload):
        events = [{"event_id": "e1"}]
        results = [EventResult(status="SUCCESS")]
        await events_ack(
            events,
            results,
            app_name="a",
            workflow_name="w",
            workflow_run_id="r1",
        )
        buf = mock_upload.await_args.kwargs["content"]
        arrow_back = pq.read_table(io.BytesIO(buf))
        self.assertEqual(arrow_back.schema.field("error_message").nullable, True)


class TestEventsAckPathValidation(unittest.IsolatedAsyncioTestCase):
    """Each call must reject untrusted-shaped path components."""

    async def _ack(self, **overrides):
        kwargs = dict(
            events=[{"event_id": "e1"}],
            results=[EventResult(status="SUCCESS")],
            app_name="a",
            workflow_name="w",
            workflow_run_id="r1",
            filename="events_ack.parquet",
        )
        kwargs.update(overrides)
        return await events_ack(**kwargs)

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_reject_app_name_with_slash(self, mock_upload):
        with self.assertRaises(ValueError):
            await self._ack(app_name="../etc/passwd")
        mock_upload.assert_not_awaited()

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_reject_app_name_with_dot(self, mock_upload):
        # Dots in app/workflow names would mess up the date segment.
        with self.assertRaises(ValueError):
            await self._ack(app_name="a.b")
        mock_upload.assert_not_awaited()

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_reject_workflow_name_with_slash(self, mock_upload):
        with self.assertRaises(ValueError):
            await self._ack(workflow_name="w/x")
        mock_upload.assert_not_awaited()

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_reject_filename_with_slash(self, mock_upload):
        with self.assertRaises(ValueError):
            await self._ack(filename="../leak.parquet")
        mock_upload.assert_not_awaited()

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_reject_filename_without_extension(self, mock_upload):
        with self.assertRaises(ValueError):
            await self._ack(filename="noext")
        mock_upload.assert_not_awaited()

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_reject_run_id_path_traversal(self, mock_upload):
        with self.assertRaises(ValueError):
            await self._ack(workflow_run_id="../../../etc")
        mock_upload.assert_not_awaited()

    @patch(
        "application_sdk.lakehouse.events_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_accept_valid_components(self, mock_upload):
        # Identifiers, hyphens, underscores all accepted.
        await self._ack(
            app_name="databricks-events",
            workflow_name="reverse_sync",
            filename="events_ack.parquet",
        )
        mock_upload.assert_awaited_once()
