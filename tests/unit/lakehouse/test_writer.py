import logging
import unittest
from unittest.mock import MagicMock

import pyarrow as pa
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, StringType

from application_sdk.lakehouse.catalog_client import CatalogClient
from application_sdk.lakehouse.writer import LakehouseWriter

_SCHEMA = Schema(
    NestedField(1, "event_id", StringType(), required=True),
    NestedField(2, "payload", StringType(), required=False),
)


class TestLakehouseWriter(unittest.TestCase):
    def setUp(self):
        self.mock_catalog = MagicMock()
        self.client = CatalogClient(self.mock_catalog)
        self.writer = LakehouseWriter(self.client, app_namespace="apps.databricks")

        self.mock_table = MagicMock()
        self.mock_catalog.load_table.return_value = self.mock_table

    def test_app_namespace_property(self):
        self.assertEqual(self.writer.app_namespace, "apps.databricks")

    def test_append_calls_table_append(self):
        arrow = pa.table({"event_id": ["e1"], "payload": ["x"]})
        rows = self.writer.append("audit", arrow)
        self.assertEqual(rows, 1)
        self.mock_table.append.assert_called_once()
        self.mock_catalog.load_table.assert_called_with(("apps", "databricks", "audit"))

    def test_append_skips_when_arrow_empty(self):
        arrow = pa.table({"event_id": pa.array([], type=pa.string())})
        rows = self.writer.append("audit", arrow)
        self.assertEqual(rows, 0)
        self.mock_table.append.assert_not_called()

    def test_cross_namespace_append_warns_but_proceeds(self):
        arrow = pa.table({"event_id": ["e1"]})
        with self.assertLogs(
            "application_sdk.lakehouse.writer", level="WARNING"
        ) as captured:
            self.writer.append("evil", arrow, namespace="other_app")
        self.assertTrue(
            any("Cross-namespace write" in m for m in captured.output),
            captured.output,
        )
        self.mock_catalog.load_table.assert_called_with(("other_app", "evil"))
        self.mock_table.append.assert_called_once()

    def test_create_or_get_table_returns_existing(self):
        result = self.writer.create_or_get_table("audit", _SCHEMA)
        self.assertIs(result, self.mock_table)
        self.mock_catalog.create_table.assert_not_called()

    def test_create_or_get_table_creates_when_missing(self):
        self.mock_catalog.load_table.side_effect = Exception("not found")
        new_table = MagicMock()
        self.mock_catalog.create_table.return_value = new_table

        result = self.writer.create_or_get_table("audit", _SCHEMA)

        self.assertIs(result, new_table)
        self.mock_catalog.create_namespace.assert_called_once_with(
            ("apps", "databricks")
        )
        self.mock_catalog.create_table.assert_called_once()
        kwargs = self.mock_catalog.create_table.call_args.kwargs
        self.assertEqual(kwargs["identifier"], ("apps", "databricks", "audit"))
        self.assertIs(kwargs["schema"], _SCHEMA)

    def test_write_records_creates_and_appends(self):
        self.mock_catalog.load_table.side_effect = [Exception("not found"), None]
        new_table = MagicMock()
        self.mock_catalog.create_table.return_value = new_table

        rows = self.writer.write_records(
            "audit",
            [{"event_id": "e1", "payload": "p"}],
            _SCHEMA,
        )
        self.assertEqual(rows, 1)
        new_table.append.assert_called_once()

    def test_write_records_returns_zero_for_empty(self):
        self.assertEqual(self.writer.write_records("audit", [], _SCHEMA), 0)
        self.mock_catalog.create_table.assert_not_called()


class TestLakehouseWriterCrossNamespaceWarning(unittest.TestCase):
    def test_warning_includes_both_namespaces(self):
        client = CatalogClient(MagicMock())
        writer = LakehouseWriter(client, app_namespace="my_app")

        logger = logging.getLogger("application_sdk.lakehouse.writer")
        with self.assertLogs(logger, level="WARNING") as captured:
            writer._check_namespace("not_my_app")
        msg = captured.output[0]
        self.assertIn("my_app", msg)
        self.assertIn("not_my_app", msg)
