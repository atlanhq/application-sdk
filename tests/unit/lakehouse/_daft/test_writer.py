"""Tests for the Daft-based bulk writer."""

import sys
import types
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


def _stub_daft_with(num_rows_per_partition):
    """Build a fake daft module whose read_parquet → df.write_iceberg → result_df chain
    yields ``num_rows_per_partition`` per partition. Returns ``(daft_module, df)``.
    """
    df = MagicMock()
    result = MagicMock()
    result.to_pylist.return_value = [{"num_rows": n} for n in num_rows_per_partition]
    df.write_iceberg.return_value = result

    daft_mod = types.ModuleType("daft")
    daft_mod.read_parquet = MagicMock(return_value=df)  # type: ignore[attr-defined]
    return daft_mod, df


class _DaftPatch:
    """Context manager: inject a fake daft module into sys.modules."""

    def __init__(self, daft_mod):
        self._daft_mod = daft_mod
        self._orig = None

    def __enter__(self):
        self._orig = sys.modules.get("daft")
        sys.modules["daft"] = self._daft_mod
        return self

    def __exit__(self, *_):
        if self._orig is None:
            sys.modules.pop("daft", None)
        else:
            sys.modules["daft"] = self._orig


class TestWriteBulk(unittest.TestCase):
    def test_writes_to_existing_table(self):
        catalog = MagicMock()
        existing = MagicMock()
        catalog.load_table.return_value = existing
        daft_mod, df = _stub_daft_with([42])

        with _DaftPatch(daft_mod):
            rows = daft_writer.write_bulk(
                catalog,
                "apps.databricks",
                "audit",
                "local/staging/run-001/",
                mode="append",
            )

        self.assertEqual(rows, 42)
        daft_mod.read_parquet.assert_called_once_with("local/staging/run-001/")
        df.write_iceberg.assert_called_once_with(existing, mode="append")

    def test_overwrite_mode_passes_through(self):
        catalog = MagicMock()
        catalog.load_table.return_value = MagicMock()
        daft_mod, df = _stub_daft_with([1])

        with _DaftPatch(daft_mod):
            daft_writer.write_bulk(catalog, "ns", "t", "prefix/", mode="overwrite")

        self.assertEqual(df.write_iceberg.call_args.kwargs["mode"], "overwrite")

    @patch("application_sdk.lakehouse._daft.writer._ops.ensure_table")
    def test_creates_table_when_missing_with_schema(self, ensure):
        catalog = MagicMock()
        catalog.load_table.side_effect = NoSuchTableError("nope")
        new_table = MagicMock()
        ensure.return_value = new_table
        daft_mod, df = _stub_daft_with([3])

        with _DaftPatch(daft_mod):
            rows = daft_writer.write_bulk(catalog, "ns", "t", "prefix/", schema=_SCHEMA)

        self.assertEqual(rows, 3)
        ensure.assert_called_once()
        df.write_iceberg.assert_called_once_with(new_table, mode="append")

    def test_raises_when_missing_and_no_schema(self):
        catalog = MagicMock()
        catalog.load_table.side_effect = NoSuchTableError("nope")
        daft_mod, _ = _stub_daft_with([0])
        with _DaftPatch(daft_mod), self.assertRaises(NoSuchTableError):
            daft_writer.write_bulk(catalog, "ns", "t", "prefix/", schema=None)

    def test_sums_num_rows_across_result_partitions(self):
        catalog = MagicMock()
        catalog.load_table.return_value = MagicMock()
        daft_mod, _ = _stub_daft_with([10, 25, 7])

        with _DaftPatch(daft_mod):
            rows = daft_writer.write_bulk(catalog, "ns", "t", "prefix/")

        self.assertEqual(rows, 42)

    def test_raises_install_hint_when_daft_missing(self):
        # No daft module in sys.modules → _import_daft should ImportError
        with _DaftPatch(None):  # injecting None will pop daft from sys.modules
            sys.modules.pop("daft", None)
            with patch.dict(sys.modules, {"daft": None}):
                with self.assertRaises(ImportError) as ctx:
                    daft_writer.write_bulk(MagicMock(), "ns", "t", "prefix/")
                self.assertIn("[lakehouse-bulk]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
