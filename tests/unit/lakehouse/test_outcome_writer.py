import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse.models import OutcomeRow, ProcessingResult
from application_sdk.lakehouse.outcome_writer import OutcomeWriter


class TestOutcomeWriter(unittest.TestCase):
    def setUp(self):
        self.mock_table = MagicMock()
        self.writer = OutcomeWriter(self.mock_table)

    def test_add_outcome_buffers_row(self):
        event = {"event_id": "e1", "asset_id": "a1"}
        result = ProcessingResult(status="SUCCESS")
        self.writer.add_outcome(event, result, retry_count=0)
        self.assertEqual(len(self.writer._buffer), 1)

    def test_flush_empty_buffer_returns_zero(self):
        self.assertEqual(self.writer.flush(), 0)

    @patch("application_sdk.lakehouse.outcome_writer.daft")
    def test_flush_writes_to_iceberg(self, mock_daft):
        mock_df = MagicMock()
        mock_daft.from_pylist.return_value = mock_df

        event = {"event_id": "e1", "asset_id": "a1"}
        result = ProcessingResult(status="SUCCESS")
        self.writer.add_outcome(event, result, retry_count=0)

        count = self.writer.flush()
        self.assertEqual(count, 1)
        mock_daft.from_pylist.assert_called_once()
        mock_df.write_iceberg.assert_called_once_with(self.mock_table, mode="append")
        self.assertEqual(len(self.writer._buffer), 0)

    @patch("application_sdk.lakehouse.outcome_writer.daft")
    def test_flush_clears_buffer(self, mock_daft):
        mock_df = MagicMock()
        mock_daft.from_pylist.return_value = mock_df

        for i in range(3):
            event = {"event_id": f"e{i}", "asset_id": f"a{i}"}
            self.writer.add_outcome(event, ProcessingResult(status="SUCCESS"), retry_count=0)

        self.assertEqual(self.writer.flush(), 3)
        self.assertEqual(len(self.writer._buffer), 0)

    def test_retry_increments_count(self):
        event = {"event_id": "e1", "asset_id": "a1"}
        result = ProcessingResult(status="RETRY", error_message="timeout")
        self.writer.add_outcome(event, result, retry_count=2)
        row = self.writer._buffer[0]
        self.assertEqual(row["retry_count"], 3)  # 2 + 1

    def test_success_preserves_count(self):
        event = {"event_id": "e1", "asset_id": "a1"}
        result = ProcessingResult(status="SUCCESS")
        self.writer.add_outcome(event, result, retry_count=2)
        row = self.writer._buffer[0]
        self.assertEqual(row["retry_count"], 2)  # unchanged

    def test_outcome_row_from_event(self):
        event = {"event_id": "e1", "asset_id": "a1"}
        result = ProcessingResult(status="RETRY", error_message="timeout")
        row = OutcomeRow.from_event(event, result, retry_count=2)
        self.assertEqual(row.event_id, "e1")
        self.assertEqual(row.asset_id, "a1")
        self.assertEqual(row.status, "RETRY")
        self.assertEqual(row.kind, "OUTCOME")
        self.assertEqual(row.retry_count, 2)
        self.assertEqual(row.error_message, "timeout")
        self.assertIsNone(row.payload)
        self.assertIsNone(row.kafka_partition)
