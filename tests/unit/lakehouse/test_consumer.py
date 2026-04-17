"""Tests for LakehouseConsumer poll loop."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pyarrow as pa

from application_sdk.lakehouse.consumer import LakehouseConsumer
from application_sdk.lakehouse.models import ProcessingResult


class FakeProcessor:
    def __init__(self, results: dict[str, ProcessingResult] | None = None):
        self._results = results or {}
        self.setup_called = False
        self.teardown_called = False
        self.processed_batches: list[list[dict]] = []

    async def setup(self):
        self.setup_called = True

    async def teardown(self):
        self.teardown_called = True

    async def process_batch(self, events):
        self.processed_batches.append(events)
        return [
            self._results.get(
                event["event_id"],
                ProcessingResult(status="SUCCESS"),
            )
            for event in events
        ]


class TestLakehouseConsumer(unittest.IsolatedAsyncioTestCase):
    def _make_consumer(self, processor=None):
        mock_catalog = MagicMock()
        return LakehouseConsumer(
            processor=processor or FakeProcessor(),
            catalog=mock_catalog,
            namespace="metadata_actions",
            table_name="test_table",
        )

    @patch("application_sdk.lakehouse.consumer.PolarisCatalogClient")
    @patch("application_sdk.lakehouse.consumer.WorkQueryRunner")
    @patch("application_sdk.lakehouse.consumer.OutcomeWriter")
    async def test_calls_setup_and_teardown(
        self, mock_ow_cls, mock_wq_cls, mock_cc_cls
    ):
        processor = FakeProcessor()
        consumer = self._make_consumer(processor)

        mock_cc_cls.return_value.has_changed.return_value = False

        await consumer.run(max_duration_seconds=0.1, poll_interval_seconds=0.05)

        self.assertTrue(processor.setup_called)
        self.assertTrue(processor.teardown_called)

    @patch("application_sdk.lakehouse.consumer.PolarisCatalogClient")
    @patch("application_sdk.lakehouse.consumer.WorkQueryRunner")
    @patch("application_sdk.lakehouse.consumer.OutcomeWriter")
    async def test_processes_batch_when_snapshot_changes(
        self, mock_ow_cls, mock_wq_cls, mock_cc_cls
    ):
        processor = FakeProcessor()
        consumer = self._make_consumer(processor)

        mock_cc_cls.return_value.has_changed.side_effect = [True, False]
        mock_cc_cls.return_value.get_current_snapshot_id.return_value = 100

        mock_table = MagicMock()
        mock_scan = MagicMock()
        mock_arrow = pa.table(
            {
                "event_id": ["e1", "e2"],
                "asset_id": ["a1", "a2"],
                "ingested_at": [datetime.now(timezone.utc)] * 2,
                "payload": ['{"desc": "hi"}', '{"desc": "bye"}'],
                "status": ["PENDING", "PENDING"],
                "kind": ["EVENT", "EVENT"],
                "retry_count": [0, 0],
                "kafka_partition": [0, 0],
                "kafka_offset": [1, 2],
                "error_message": [None, None],
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
            },
            {
                "event_id": "e2",
                "asset_id": "a2",
                "payload": '{"desc": "bye"}',
                "current_retry_count": 0,
            },
        ]

        mock_ow_cls.return_value.flush.return_value = 2

        await consumer.run(max_duration_seconds=0.2, poll_interval_seconds=0.05)

        self.assertEqual(len(processor.processed_batches), 1)
        self.assertEqual(len(processor.processed_batches[0]), 2)
        self.assertEqual(processor.processed_batches[0][0]["event_id"], "e1")

    @patch("application_sdk.lakehouse.consumer.PolarisCatalogClient")
    @patch("application_sdk.lakehouse.consumer.WorkQueryRunner")
    @patch("application_sdk.lakehouse.consumer.OutcomeWriter")
    async def test_unhandled_exception_marks_all_retry(
        self, mock_ow_cls, mock_wq_cls, mock_cc_cls
    ):
        class FailingProcessor(FakeProcessor):
            async def process_batch(self, events):
                raise RuntimeError("kaboom")

        processor = FailingProcessor()
        consumer = self._make_consumer(processor)

        mock_cc_cls.return_value.has_changed.side_effect = [True, False]
        mock_cc_cls.return_value.get_current_snapshot_id.return_value = 100

        mock_table = MagicMock()
        mock_scan = MagicMock()
        mock_arrow = pa.table(
            {
                "event_id": ["e1"],
                "asset_id": ["a1"],
                "ingested_at": [datetime.now(timezone.utc)],
                "payload": ["{}"],
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
                "payload": "{}",
                "current_retry_count": 0,
            },
        ]
        mock_ow_cls.return_value.flush.return_value = 1

        await consumer.run(max_duration_seconds=0.2, poll_interval_seconds=0.05)

        mock_ow_cls.return_value.add_outcome.assert_called_once()
        call_args = mock_ow_cls.return_value.add_outcome.call_args
        self.assertEqual(call_args[0][1].status, "RETRY")
