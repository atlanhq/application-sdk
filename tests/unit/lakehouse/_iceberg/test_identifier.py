"""Internal tests for Iceberg-format identifier and namespace helpers."""

import unittest
from unittest.mock import MagicMock

from application_sdk.lakehouse._iceberg import identifier as ident


class TestIdentifier(unittest.TestCase):
    def test_flat(self):
        self.assertEqual(ident.identifier("samples", "events"), ("samples", "events"))

    def test_dotted(self):
        self.assertEqual(
            ident.identifier("apps.databricks", "audit"),
            ("apps", "databricks", "audit"),
        )

    def test_strips_empty(self):
        self.assertEqual(
            ident.identifier(".apps..databricks.", "t"),
            ("apps", "databricks", "t"),
        )


class TestEnsureNamespace(unittest.TestCase):
    def test_swallows_already_exists(self):
        catalog = MagicMock()
        catalog.create_namespace.side_effect = Exception("exists")
        ident.ensure_namespace(catalog, "samples")
        catalog.create_namespace.assert_called_once_with(("samples",))

    def test_passes_tuple_for_dotted(self):
        catalog = MagicMock()
        ident.ensure_namespace(catalog, "apps.databricks")
        catalog.create_namespace.assert_called_once_with(("apps", "databricks"))


if __name__ == "__main__":
    unittest.main()
