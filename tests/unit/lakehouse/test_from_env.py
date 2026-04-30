"""Tests for the env-driven factory helpers."""

import os
import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse import (
    CatalogClient,
    LakehouseReader,
    LakehouseWriter,
    load_catalog_from_env,
)


class TestLoadCatalogFromEnv(unittest.TestCase):
    def setUp(self):
        for k in (
            "ICEBERG_CATALOG_URI",
            "ICEBERG_CLIENT_ID",
            "ICEBERG_CLIENT_SECRET",
            "ICEBERG_WAREHOUSE",
        ):
            os.environ.pop(k, None)

    def test_raises_when_missing_uri(self):
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        with self.assertRaises(RuntimeError) as ctx:
            load_catalog_from_env()
        self.assertIn("ICEBERG_CATALOG_URI", str(ctx.exception))

    def test_raises_when_missing_client_id(self):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        with self.assertRaises(RuntimeError) as ctx:
            load_catalog_from_env()
        self.assertIn("ICEBERG_CLIENT_ID", str(ctx.exception))

    def test_raises_when_missing_client_secret(self):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        with self.assertRaises(RuntimeError) as ctx:
            load_catalog_from_env()
        self.assertIn("ICEBERG_CLIENT_SECRET", str(ctx.exception))

    @patch("pyiceberg.catalog.load_catalog")
    def test_loads_catalog_with_default_warehouse(self, load_catalog):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        load_catalog.return_value = MagicMock()

        load_catalog_from_env()

        args, kwargs = load_catalog.call_args
        self.assertEqual(args, ("context_store",))
        self.assertEqual(kwargs["uri"], "http://x")
        self.assertEqual(kwargs["warehouse"], "context_store")
        self.assertEqual(kwargs["credential"], "id:secret")
        self.assertEqual(kwargs["type"], "rest")

    @patch("pyiceberg.catalog.load_catalog")
    def test_uses_custom_warehouse(self, load_catalog):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        os.environ["ICEBERG_WAREHOUSE"] = "my_warehouse"
        load_catalog.return_value = MagicMock()

        load_catalog_from_env()

        kwargs = load_catalog.call_args.kwargs
        self.assertEqual(kwargs["warehouse"], "my_warehouse")


class TestFromEnvFactories(unittest.TestCase):
    @patch("application_sdk.lakehouse.catalog_client.load_catalog_from_env")
    def test_catalog_client_from_env(self, load):
        load.return_value = MagicMock()
        client = CatalogClient.from_env()
        self.assertIsInstance(client, CatalogClient)
        load.assert_called_once()

    @patch("application_sdk.lakehouse.catalog_client.load_catalog_from_env")
    def test_lakehouse_reader_from_env(self, load):
        load.return_value = MagicMock()
        reader = LakehouseReader.from_env()
        self.assertIsInstance(reader, LakehouseReader)
        load.assert_called_once()

    @patch("application_sdk.lakehouse.catalog_client.load_catalog_from_env")
    def test_lakehouse_writer_from_env_binds_namespace(self, load):
        load.return_value = MagicMock()
        writer = LakehouseWriter.from_env(app_namespace="apps.databricks")
        self.assertEqual(writer.app_namespace, "apps.databricks")
        load.assert_called_once()


if __name__ == "__main__":
    unittest.main()
