import unittest
from unittest.mock import MagicMock

import pyarrow as pa

from application_sdk.lakehouse.catalog_client import CatalogClient
from application_sdk.lakehouse.reader import LakehouseReader


def _arrow(rows):
    if not rows:
        return pa.table({"event_id": pa.array([], type=pa.string())})
    return pa.Table.from_pylist(rows)


class TestLakehouseReader(unittest.TestCase):
    def setUp(self):
        self.mock_catalog = MagicMock()
        self.client = CatalogClient(self.mock_catalog)
        self.reader = LakehouseReader(self.client)

        self.mock_table = MagicMock()
        self.mock_scan = MagicMock()
        self.mock_table.scan.return_value = self.mock_scan
        self.mock_catalog.load_table.return_value = self.mock_table

    def test_read_arrow_passes_filter_to_scan(self):
        self.mock_scan.to_arrow.return_value = _arrow([])
        self.reader.read_arrow(
            "automation_engine", "events", row_filter="status = 'unprocessed'"
        )
        self.mock_table.scan.assert_called_once_with(
            row_filter="status = 'unprocessed'", selected_fields=("*",), limit=None
        )

    def test_read_arrow_default_filter_when_none(self):
        self.mock_scan.to_arrow.return_value = _arrow([])
        self.reader.read_arrow("automation_engine", "events")
        # 'true' = unfiltered scan
        self.mock_table.scan.assert_called_once_with(
            row_filter="true", selected_fields=("*",), limit=None
        )

    def test_fetch_records_returns_dicts_sorted(self):
        self.mock_scan.to_arrow.return_value = _arrow(
            [
                {"event_id": "b", "received_at": 2},
                {"event_id": "a", "received_at": 1},
                {"event_id": "c", "received_at": 3},
            ]
        )
        records = self.reader.fetch_records(
            "automation_engine", "events", sort_by="received_at"
        )
        self.assertEqual([r["event_id"] for r in records], ["a", "b", "c"])

    def test_fetch_records_returns_empty_for_empty_scan(self):
        self.mock_scan.to_arrow.return_value = _arrow([])
        self.assertEqual(self.reader.fetch_records("automation_engine", "events"), [])

    def test_fetch_records_applies_limit(self):
        self.mock_scan.to_arrow.return_value = _arrow(
            [{"event_id": str(i), "received_at": i} for i in range(5)]
        )
        records = self.reader.fetch_records(
            "automation_engine", "events", limit=2, sort_by="received_at"
        )
        self.assertEqual(len(records), 2)

    def test_current_snapshot_id_returns_id(self):
        snap = MagicMock()
        snap.snapshot_id = 12345
        self.mock_table.current_snapshot.return_value = snap
        self.assertEqual(self.reader.current_snapshot_id("ns", "t"), 12345)

    def test_current_snapshot_id_none_for_empty_table(self):
        self.mock_table.current_snapshot.return_value = None
        self.assertIsNone(self.reader.current_snapshot_id("ns", "t"))

    def test_reads_from_arbitrary_namespace(self):
        self.mock_scan.to_arrow.return_value = _arrow([])
        self.reader.read_arrow("automation_engine", "events")
        self.mock_catalog.load_table.assert_called_with(("automation_engine", "events"))
