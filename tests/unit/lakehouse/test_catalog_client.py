import unittest
from unittest.mock import MagicMock

from application_sdk.lakehouse.catalog_client import CatalogClient


class TestCatalogClient(unittest.TestCase):
    def setUp(self):
        self.mock_catalog = MagicMock()
        self.client = CatalogClient(self.mock_catalog)

    def test_identifier_handles_flat_namespace(self):
        self.assertEqual(
            CatalogClient.identifier("samples", "events"), ("samples", "events")
        )

    def test_identifier_splits_dotted_namespace(self):
        self.assertEqual(
            CatalogClient.identifier("apps.databricks", "audit"),
            ("apps", "databricks", "audit"),
        )

    def test_identifier_strips_empty_parts(self):
        self.assertEqual(
            CatalogClient.identifier(".apps..databricks.", "t"),
            ("apps", "databricks", "t"),
        )

    def test_load_table_uses_identifier_tuple(self):
        self.client.load_table("apps.databricks", "audit")
        self.mock_catalog.load_table.assert_called_once_with(
            ("apps", "databricks", "audit")
        )

    def test_table_exists_true(self):
        self.mock_catalog.load_table.return_value = MagicMock()
        self.assertTrue(self.client.table_exists("samples", "events"))

    def test_table_exists_false_on_load_failure(self):
        self.mock_catalog.load_table.side_effect = Exception("not found")
        self.assertFalse(self.client.table_exists("samples", "events"))

    def test_ensure_namespace_swallows_already_exists(self):
        self.mock_catalog.create_namespace.side_effect = Exception("exists")
        self.client.ensure_namespace("samples")
        self.mock_catalog.create_namespace.assert_called_once_with(("samples",))

    def test_ensure_namespace_passes_tuple_for_dotted(self):
        self.client.ensure_namespace("apps.databricks")
        self.mock_catalog.create_namespace.assert_called_once_with(
            ("apps", "databricks")
        )
