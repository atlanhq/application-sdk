import unittest
from unittest.mock import MagicMock

from application_sdk.lakehouse.catalog_client import PolarisCatalogClient


class TestPolarisCatalogClient(unittest.TestCase):
    def setUp(self):
        self.mock_catalog = MagicMock()
        self.client = PolarisCatalogClient(
            catalog=self.mock_catalog,
            namespace="metadata_actions",
            table_name="test_table",
        )

    def test_get_current_snapshot_id_returns_id(self):
        mock_table = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.snapshot_id = 12345
        mock_table.current_snapshot.return_value = mock_snapshot
        self.mock_catalog.load_table.return_value = mock_table
        self.assertEqual(self.client.get_current_snapshot_id(), 12345)

    def test_get_current_snapshot_id_returns_none_when_no_snapshot(self):
        mock_table = MagicMock()
        mock_table.current_snapshot.return_value = None
        self.mock_catalog.load_table.return_value = mock_table
        self.assertIsNone(self.client.get_current_snapshot_id())

    def test_has_changed_true_when_different(self):
        mock_table = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.snapshot_id = 200
        mock_table.current_snapshot.return_value = mock_snapshot
        self.mock_catalog.load_table.return_value = mock_table
        self.assertTrue(self.client.has_changed(100))

    def test_has_changed_false_when_same(self):
        mock_table = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.snapshot_id = 100
        mock_table.current_snapshot.return_value = mock_snapshot
        self.mock_catalog.load_table.return_value = mock_table
        self.assertFalse(self.client.has_changed(100))

    def test_has_changed_true_when_last_was_none(self):
        mock_table = MagicMock()
        mock_snapshot = MagicMock()
        mock_snapshot.snapshot_id = 100
        mock_table.current_snapshot.return_value = mock_snapshot
        self.mock_catalog.load_table.return_value = mock_table
        self.assertTrue(self.client.has_changed(None))

    def test_has_changed_false_when_table_empty(self):
        mock_table = MagicMock()
        mock_table.current_snapshot.return_value = None
        self.mock_catalog.load_table.return_value = mock_table
        self.assertFalse(self.client.has_changed(None))
