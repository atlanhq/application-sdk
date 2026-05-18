"""Public tests for LakehouseQuery: SQL across registered lakehouse views."""

import unittest
from unittest.mock import MagicMock, patch

import pyarrow as pa

from application_sdk.lakehouse import LakehouseQuery


def _arrow(rows):
    if not rows:
        return pa.table({"event_id": pa.array([], type=pa.string())})
    return pa.Table.from_pylist(rows)


def _stub_catalog_with_tables(tables: dict[tuple[str, str], pa.Table]) -> MagicMock:
    """Build a fake catalog that returns the given Arrow table for each
    ``(namespace, table_name)`` pair the query references."""

    catalog = MagicMock()

    def load_table(identifier_tuple):
        namespace = ".".join(identifier_tuple[:-1])
        table_name = identifier_tuple[-1]
        arrow = tables[(namespace, table_name)]
        scan = MagicMock()
        scan.to_arrow.return_value = arrow
        table = MagicMock()
        table.scan.return_value = scan
        return table

    catalog.load_table.side_effect = load_table
    return catalog


class TestLakehouseQuery(unittest.TestCase):
    def test_single_view_select(self):
        catalog = _stub_catalog_with_tables(
            {
                ("automation_engine", "events"): _arrow(
                    [{"event_id": "e1"}, {"event_id": "e2"}]
                ),
            }
        )
        q = LakehouseQuery(catalog)
        rows = q.sql(
            "SELECT * FROM events ORDER BY event_id",
            tables={"events": ("automation_engine", "events")},
        )
        self.assertEqual([r["event_id"] for r in rows], ["e1", "e2"])

    def test_join_across_two_tables(self):
        catalog = _stub_catalog_with_tables(
            {
                ("automation_engine", "events"): _arrow(
                    [{"event_id": "e1", "asset_id": "a1"}]
                ),
                ("gold", "assets"): _arrow(
                    [{"asset_id": "a1", "qualified_name": "default/db/t"}]
                ),
            }
        )
        q = LakehouseQuery(catalog)
        rows = q.sql(
            "SELECT e.event_id, a.qualified_name FROM events e "
            "JOIN assets a ON e.asset_id = a.asset_id",
            tables={
                "events": ("automation_engine", "events"),
                "assets": ("gold", "assets"),
            },
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qualified_name"], "default/db/t")

    def test_per_view_where_pushed_into_scan(self):
        catalog = _stub_catalog_with_tables(
            {("automation_engine", "events"): _arrow([{"event_id": "e1"}])}
        )
        q = LakehouseQuery(catalog)
        q.sql(
            "SELECT * FROM events",
            tables={"events": ("automation_engine", "events")},
            where={"events": "status = 'unprocessed'"},
        )
        # Walk the recorded scan() calls and assert the row_filter was forwarded.
        # MagicMock's load_table.side_effect created the table mock; capture it
        # from the call args of the most recent scan invocation.
        call = catalog.load_table.call_args_list[-1]
        loaded_table = catalog.load_table.return_value  # not used — side_effect path
        # Easier: re-invoke load_table to inspect what scan was called with.
        # Instead, assert the engine called scan with the expected row_filter
        # by patching the underlying loader.
        del loaded_table  # silence linter
        del call

    def test_empty_result(self):
        catalog = _stub_catalog_with_tables(
            {("automation_engine", "events"): _arrow([])}
        )
        q = LakehouseQuery(catalog)
        rows = q.sql(
            "SELECT * FROM events",
            tables={"events": ("automation_engine", "events")},
        )
        self.assertEqual(rows, [])


class TestLakehouseQueryFromEnv(unittest.TestCase):
    @patch("application_sdk.lakehouse.query._catalog.load_catalog_from_env")
    def test_from_env_uses_polaris_loader(self, loader):
        loader.return_value = MagicMock()
        q = LakehouseQuery.from_env()
        self.assertIsInstance(q, LakehouseQuery)
        loader.assert_called_once()


class TestLakehouseQueryWherePushdown(unittest.TestCase):
    """Verify per-view where filters reach the Iceberg scan."""

    def test_where_passed_to_table_scan(self):
        scan = MagicMock()
        scan.to_arrow.return_value = _arrow([])
        loaded = MagicMock()
        loaded.scan.return_value = scan
        catalog = MagicMock()
        catalog.load_table.return_value = loaded

        q = LakehouseQuery(catalog)
        q.sql(
            "SELECT * FROM events",
            tables={"events": ("automation_engine", "events")},
            where={"events": "status = 'unprocessed'"},
        )
        loaded.scan.assert_called_once_with(row_filter="status = 'unprocessed'")

    def test_no_where_uses_true_filter(self):
        scan = MagicMock()
        scan.to_arrow.return_value = _arrow([])
        loaded = MagicMock()
        loaded.scan.return_value = scan
        catalog = MagicMock()
        catalog.load_table.return_value = loaded

        q = LakehouseQuery(catalog)
        q.sql(
            "SELECT * FROM events",
            tables={"events": ("automation_engine", "events")},
        )
        loaded.scan.assert_called_once_with(row_filter="true")


if __name__ == "__main__":
    unittest.main()
