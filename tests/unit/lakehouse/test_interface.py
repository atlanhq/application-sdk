import unittest
from unittest.mock import MagicMock

from application_sdk.lakehouse.interface import LakehouseInterface
from application_sdk.lakehouse.reader import LakehouseReader
from application_sdk.lakehouse.writer import LakehouseWriter


class TestLakehouseInterface(unittest.TestCase):
    def test_constructs_reader_and_writer(self):
        catalog = MagicMock()
        interface = LakehouseInterface(catalog, app_namespace="apps.databricks")

        self.assertIsInstance(interface.reader, LakehouseReader)
        self.assertIsInstance(interface.writer, LakehouseWriter)
        self.assertEqual(interface.app_namespace, "apps.databricks")
        self.assertEqual(interface.writer.app_namespace, "apps.databricks")

    def test_reader_and_writer_share_catalog_client(self):
        catalog = MagicMock()
        interface = LakehouseInterface(catalog, app_namespace="my_app")
        self.assertIs(interface.catalog_client, interface.reader._client)
        self.assertIs(interface.catalog_client, interface.writer._client)
