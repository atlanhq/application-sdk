"""Tests for the Daft-based dataframe writer."""

import unittest
from unittest.mock import MagicMock, patch

from pyiceberg.exceptions import NoSuchTableError

from application_sdk.lakehouse._daft import writer as daft_writer
from application_sdk.lakehouse.schema import Field, Schema

_SCHEMA = Schema(
    fields=[
        Field("event_id", "string", nullable=False),
        Field("payload", "string", nullable=True),
    ]
)


def _df_with_rows(num_rows: int):
    df = MagicMock()
    result = MagicMock()
    result.to_pylist.return_value = [{"num_rows": num_rows}]
    df.write_iceberg.return_value = result
    return df


class TestWriteDataframe(unittest.TestCase):
    def test_writes_to_existing_table(self):
        catalog = MagicMock()
        existing = MagicMock()
        catalog.load_table.return_value = existing
        df = _df_with_rows(42)

        rows = daft_writer.write_dataframe(
            catalog, "apps.databricks", "audit", df, mode="append"
        )
        self.assertEqual(rows, 42)
        df.write_iceberg.assert_called_once_with(existing, mode="append")

    def test_overwrite_mode_passes_through(self):
        catalog = MagicMock()
        catalog.load_table.return_value = MagicMock()
        df = _df_with_rows(1)
        daft_writer.write_dataframe(catalog, "ns", "t", df, mode="overwrite")
        df.write_iceberg.assert_called_once()
        self.assertEqual(df.write_iceberg.call_args.kwargs["mode"], "overwrite")

    @patch("application_sdk.lakehouse._daft.writer._ops.ensure_table")
    def test_creates_table_when_missing_with_schema(self, ensure):
        catalog = MagicMock()
        catalog.load_table.side_effect = NoSuchTableError("nope")
        new_table = MagicMock()
        ensure.return_value = new_table
        df = _df_with_rows(3)

        rows = daft_writer.write_dataframe(catalog, "ns", "t", df, schema=_SCHEMA)
        self.assertEqual(rows, 3)
        ensure.assert_called_once()
        df.write_iceberg.assert_called_once_with(new_table, mode="append")

    def test_raises_when_missing_and_no_schema(self):
        catalog = MagicMock()
        catalog.load_table.side_effect = NoSuchTableError("nope")
        with self.assertRaises(NoSuchTableError):
            daft_writer.write_dataframe(
                catalog, "ns", "t", _df_with_rows(0), schema=None
            )

    def test_sums_num_rows_across_result_partitions(self):
        catalog = MagicMock()
        catalog.load_table.return_value = MagicMock()
        df = MagicMock()
        result = MagicMock()
        result.to_pylist.return_value = [
            {"num_rows": 10},
            {"num_rows": 25},
            {"num_rows": 7},
        ]
        df.write_iceberg.return_value = result
        rows = daft_writer.write_dataframe(catalog, "ns", "t", df)
        self.assertEqual(rows, 42)


if __name__ == "__main__":
    unittest.main()
