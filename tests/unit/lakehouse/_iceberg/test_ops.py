"""Internal tests for the scan / append / ensure_table primitives."""

import unittest
from unittest.mock import MagicMock, patch

import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError

from application_sdk.lakehouse._iceberg import ops
from application_sdk.lakehouse.schema import Field, PartitionBy, Schema

_SCHEMA = Schema(
    fields=[
        Field("event_id", "string", nullable=False),
        Field("payload", "string", nullable=True),
    ]
)


class TestScanRecords(unittest.TestCase):
    def test_scan_returns_dicts(self):
        catalog = MagicMock()
        table = MagicMock()
        scan = MagicMock()
        scan.to_arrow.return_value = pa.table(
            {"event_id": ["a", "b"], "payload": ["x", "y"]}
        )
        table.scan.return_value = scan
        catalog.load_table.return_value = table

        result = ops.scan_records(catalog, "ns", "t")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["event_id"], "a")

    def test_scan_passes_filter_and_limit(self):
        catalog = MagicMock()
        table = MagicMock()
        scan = MagicMock()
        scan.to_arrow.return_value = pa.table(
            {"event_id": pa.array([], type=pa.string())}
        )
        table.scan.return_value = scan
        catalog.load_table.return_value = table

        ops.scan_records(catalog, "ns", "t", where="status='unprocessed'", limit=10)
        kwargs = table.scan.call_args.kwargs
        self.assertEqual(kwargs["row_filter"], "status='unprocessed'")
        self.assertEqual(kwargs["limit"], 10)

    def test_scan_handles_empty(self):
        catalog = MagicMock()
        table = MagicMock()
        scan = MagicMock()
        scan.to_arrow.return_value = pa.table(
            {"event_id": pa.array([], type=pa.string())}
        )
        table.scan.return_value = scan
        catalog.load_table.return_value = table

        self.assertEqual(ops.scan_records(catalog, "ns", "t"), [])


class TestEnsureTable(unittest.TestCase):
    def test_returns_existing(self):
        catalog = MagicMock()
        existing = MagicMock()
        catalog.load_table.return_value = existing
        result = ops.ensure_table(catalog, "apps.databricks", "audit", _SCHEMA)
        self.assertIs(result, existing)
        catalog.create_table.assert_not_called()

    def test_creates_when_missing(self):
        catalog = MagicMock()
        catalog.load_table.side_effect = NoSuchTableError("nope")
        new_table = MagicMock()
        catalog.create_table.return_value = new_table
        result = ops.ensure_table(catalog, "apps.databricks", "audit", _SCHEMA)
        self.assertIs(result, new_table)
        catalog.create_namespace.assert_called_once_with(("apps", "databricks"))
        kwargs = catalog.create_table.call_args.kwargs
        self.assertEqual(kwargs["identifier"], ("apps", "databricks", "audit"))

    def test_creates_with_partition_spec_when_declared(self):
        partitioned = Schema(
            fields=[
                Field("event_id", "string", nullable=False),
                Field("ingested_at", "timestamp", nullable=False),
            ],
            partition_by=PartitionBy("ingested_at", transform="day"),
        )
        catalog = MagicMock()
        catalog.load_table.side_effect = NoSuchTableError("nope")
        catalog.create_table.return_value = MagicMock()
        ops.ensure_table(catalog, "apps", "events", partitioned)
        kwargs = catalog.create_table.call_args.kwargs
        self.assertIn("partition_spec", kwargs)


class TestAppendRecords(unittest.TestCase):
    @patch("application_sdk.lakehouse._iceberg.ops.ensure_table")
    def test_appends_with_schema_creates(self, ensure):
        catalog = MagicMock()
        table = MagicMock()
        ensure.return_value = table
        rows = ops.append_records(
            catalog,
            "apps.databricks",
            "audit",
            [{"event_id": "e1", "payload": "x"}],
            schema=_SCHEMA,
        )
        self.assertEqual(rows, 1)
        ensure.assert_called_once()
        table.append.assert_called_once()

    def test_appends_without_schema_uses_table_schema(self):
        catalog = MagicMock()
        table = MagicMock()
        # When no schema provided, use table.schema().as_arrow()
        table.schema.return_value.as_arrow.return_value = pa.schema(
            [pa.field("event_id", pa.string()), pa.field("payload", pa.string())]
        )
        catalog.load_table.return_value = table
        rows = ops.append_records(
            catalog,
            "apps.databricks",
            "audit",
            [{"event_id": "e1", "payload": "x"}],
            schema=None,
        )
        self.assertEqual(rows, 1)
        table.append.assert_called_once()

    def test_empty_records_zero(self):
        catalog = MagicMock()
        rows = ops.append_records(catalog, "ns", "t", [], schema=None)
        self.assertEqual(rows, 0)


if __name__ == "__main__":
    unittest.main()
