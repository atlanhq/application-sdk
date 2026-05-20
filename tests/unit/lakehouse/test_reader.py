"""Public tests for LakehouseReader: dict primitives only."""

import unittest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse import LakehouseReader


def _make_reader_with_records(records):
    reader = LakehouseReader(MagicMock())
    return reader, records


class TestLakehouseReader(unittest.TestCase):
    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_returns_dicts(self, scan):
        scan.return_value = [
            {"event_id": "e1", "received_at": 1},
            {"event_id": "e2", "received_at": 2},
        ]
        reader = LakehouseReader(MagicMock())
        records = reader.fetch_records("automation_engine", "events")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["event_id"], "e1")

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_default_filter_is_none(self, scan):
        scan.return_value = []
        reader = LakehouseReader(MagicMock())
        reader.fetch_records("ns", "t")
        kwargs = scan.call_args.kwargs
        self.assertIsNone(kwargs["where"])

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_passes_where(self, scan):
        scan.return_value = []
        reader = LakehouseReader(MagicMock())
        reader.fetch_records("ns", "t", where="status = 'unprocessed'")
        kwargs = scan.call_args.kwargs
        self.assertEqual(kwargs["where"], "status = 'unprocessed'")

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_sorts_in_process(self, scan):
        now = datetime.now(UTC)
        scan.return_value = [
            {"event_id": "b", "received_at": 2},
            {"event_id": "a", "received_at": 1},
            {"event_id": "c", "received_at": 3},
        ]
        reader = LakehouseReader(MagicMock())
        records = reader.fetch_records("ns", "t", sort_by="received_at")
        self.assertEqual([r["event_id"] for r in records], ["a", "b", "c"])

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_applies_limit_after_sort(self, scan):
        scan.return_value = [{"event_id": str(i), "received_at": i} for i in range(5)]
        reader = LakehouseReader(MagicMock())
        records = reader.fetch_records("ns", "t", limit=2, sort_by="received_at")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["event_id"], "0")

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_does_not_push_limit_to_scan_when_sorted(self, scan):
        """When sort_by is set, the SDK must read the full filtered result
        before slicing — otherwise we'd sort the wrong N rows."""
        scan.return_value = []
        reader = LakehouseReader(MagicMock())
        reader.fetch_records("ns", "t", limit=10, sort_by="received_at")
        # scan should have been called with limit=None, not limit=10
        self.assertIsNone(scan.call_args.kwargs["limit"])

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_fetch_records_pushes_limit_to_scan_when_unsorted(self, scan):
        scan.return_value = []
        reader = LakehouseReader(MagicMock())
        reader.fetch_records("ns", "t", limit=10)
        self.assertEqual(scan.call_args.kwargs["limit"], 10)

    @patch("application_sdk.lakehouse.reader._ops.scan_records")
    def test_passes_select(self, scan):
        scan.return_value = []
        reader = LakehouseReader(MagicMock())
        reader.fetch_records("ns", "t", select=("event_id", "status"))
        kwargs = scan.call_args.kwargs
        self.assertEqual(kwargs["select"], ("event_id", "status"))

    @patch("application_sdk.lakehouse.reader._ops.current_snapshot_id")
    def test_current_snapshot_id_delegates(self, snap):
        snap.return_value = 12345
        reader = LakehouseReader(MagicMock())
        self.assertEqual(reader.current_snapshot_id("ns", "t"), 12345)


class TestLakehouseReaderFromEnv(unittest.TestCase):
    @patch("application_sdk.lakehouse.reader._catalog.load_catalog_from_env")
    def test_from_env_uses_internal_loader(self, loader):
        loader.return_value = MagicMock()
        reader = LakehouseReader.from_env()
        self.assertIsInstance(reader, LakehouseReader)
        loader.assert_called_once()


if __name__ == "__main__":
    unittest.main()
