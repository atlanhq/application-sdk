import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa

from application_sdk.lakehouse.consumer import LakehouseConsumer
from application_sdk.lakehouse.models import ProcessingResult


class FakeProcessor:
    def __init__(self, results=None):
        self._results = results or {}
        self.setup_called = False
        self.teardown_called = False
        self.processed_events = []

    async def setup(self):
        self.setup_called = True

    async def teardown(self):
        self.teardown_called = True

    async def process_event(self, event):
        self.processed_events.append(event)
        return self._results.get(event["event_id"], ProcessingResult(status="SUCCESS"))


class TestLakehouseConsumer(unittest.IsolatedAsyncioTestCase):
    def _make_consumer(self, processor=None):
        return LakehouseConsumer(
            processor=processor or FakeProcessor(),
            catalog=MagicMock(),
            namespace="metadata_actions",
            table_name="test_table",
        )

    @patch("application_sdk.lakehouse.consumer.PolarisCatalogClient")
    @patch("application_sdk.lakehouse.consumer.WorkQueryRunner")
    @patch("application_sdk.lakehouse.consumer.OutcomeWriter")
    async def test_calls_setup_and_teardown(self, mock_ow_cls, mock_wq_cls, mock_cc_cls):
        processor = FakeProcessor()
        consumer = self._make_consumer(processor)
        mock_cc_cls.return_value.has_changed.return_value = False

        await consumer.run(max_duration_seconds=0.1, poll_interval_seconds=0.05)

        self.assertTrue(processor.setup_called)
        self.assertTrue(processor.teardown_called)

    @patch("application_sdk.lakehouse.consumer.PolarisCatalogClient")
    @patch("application_sdk.lakehouse.consumer.WorkQueryRunner")
    @patch("application_sdk.lakehouse.consumer.OutcomeWriter")
    async def test_processes_events_when_snapshot_changes(self, mock_ow_cls, mock_wq_cls, mock_cc_cls):
        processor = FakeProcessor()
        consumer = self._make_consumer(processor)

        mock_cc_cls.return_value.has_changed.side_effect = [True, False, False, False, False]
        mock_cc_cls.return_value.get_current_snapshot_id.return_value = 100

        mock_table = MagicMock()
        mock_scan = MagicMock()
        mock_arrow = pa.table(
            {
                "event_id": ["e1"],
                "asset_id": ["a1"],
                "ingested_at": [datetime.now(timezone.utc)],
                "payload": ['{"desc": "hi"}'],
                "status": ["PENDING"],
                "kind": ["EVENT"],
                "retry_count": [0],
                "kafka_partition": [0],
                "kafka_offset": [1],
                "error_message": [None],
            }
        )
        mock_scan.to_arrow.return_value = mock_arrow
        mock_table.scan.return_value = mock_scan
        mock_cc_cls.return_value.load_table.return_value = mock_table

        mock_wq_cls.return_value.run.return_value = [
            {
                "event_id": "e1",
                "asset_id": "a1",
                "payload": '{"desc": "hi"}',
                "current_retry_count": 0,
            }
        ]
        mock_ow_cls.return_value.flush.return_value = 1

        await consumer.run(max_duration_seconds=0.3, poll_interval_seconds=0.05)

        self.assertEqual(len(processor.processed_events), 1)
        self.assertEqual(processor.processed_events[0]["event_id"], "e1")
