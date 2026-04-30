import io
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pyarrow.parquet as pq

from application_sdk.lakehouse.event_ack import (
    EventAckWriter,
    _ack_path,
    _build_ack_arrow,
)
from application_sdk.lakehouse.models import ProcessingResult


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


class TestEventAckBuildArrow(unittest.TestCase):
    """Internal: ack-arrow construction. ``_build_ack_arrow`` is private."""

    def test_aligns_events_and_results(self):
        events = [{"event_id": "e1"}, {"event_id": "e2"}]
        results = [
            ProcessingResult(status="SUCCESS"),
            ProcessingResult(status="FAILED", error_message="boom"),
        ]
        arrow = _build_ack_arrow(events, results)
        self.assertEqual(arrow.num_rows, 2)
        self.assertEqual(arrow["event_id"].to_pylist(), ["e1", "e2"])
        self.assertEqual(arrow["status"].to_pylist(), ["SUCCESS", "FAILED"])
        self.assertEqual(arrow["error_message"].to_pylist(), [None, "boom"])

    def test_raises_on_length_mismatch(self):
        with self.assertRaises(ValueError):
            _build_ack_arrow(
                [{"event_id": "e1"}], [ProcessingResult(status="SUCCESS")] * 2
            )


class TestEventAckWrite(unittest.IsolatedAsyncioTestCase):
    @patch(
        "application_sdk.lakehouse.event_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_write_uploads_parquet(self, mock_upload):
        writer = EventAckWriter(app_name="databricks", workflow_name="reverse-sync")
        events = [{"event_id": "e1"}]
        results = [ProcessingResult(status="SUCCESS")]

        path = await writer.write(events, results, workflow_run_id="run-123")

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
        "application_sdk.lakehouse.event_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_custom_filename(self, mock_upload):
        writer = EventAckWriter(app_name="a", workflow_name="w", filename="ack.parquet")
        await writer.write(
            [{"event_id": "e1"}],
            [ProcessingResult(status="SUCCESS")],
            workflow_run_id="r1",
        )
        kwargs = mock_upload.await_args.kwargs
        self.assertTrue(kwargs["key"].endswith("/r1/ack.parquet"))

    @patch(
        "application_sdk.lakehouse.event_ack.upload_file_from_bytes",
        new_callable=AsyncMock,
    )
    async def test_uses_arrow_schema_with_nullable_error_message(self, mock_upload):
        writer = EventAckWriter(app_name="a", workflow_name="w")
        events = [{"event_id": "e1"}]
        results = [ProcessingResult(status="SUCCESS")]
        await writer.write(events, results, workflow_run_id="r1")
        buf = mock_upload.await_args.kwargs["content"]
        arrow_back = pq.read_table(io.BytesIO(buf))
        self.assertEqual(arrow_back.schema.field("error_message").nullable, True)
