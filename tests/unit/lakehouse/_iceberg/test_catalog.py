"""Internal tests for the env-driven catalog loader."""

import os
import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse._iceberg import catalog as cat

_ENV_KEYS = (
    "ICEBERG_CATALOG_URI",
    "ICEBERG_CLIENT_ID",
    "ICEBERG_CLIENT_SECRET",
    "ICEBERG_WAREHOUSE",
    "ATLAN_DOMAIN_NAME",
)


class TestLoadCatalogFromEnv(unittest.TestCase):
    def setUp(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k in _ENV_KEYS:
            os.environ.pop(k, None)

    def test_raises_when_no_uri_or_domain(self):
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        with self.assertRaises(RuntimeError) as ctx:
            cat.load_catalog_from_env()
        self.assertIn("ICEBERG_CATALOG_URI", str(ctx.exception))
        self.assertIn("ATLAN_DOMAIN_NAME", str(ctx.exception))

    @patch("application_sdk.lakehouse._iceberg.catalog.load_catalog")
    def test_explicit_uri_wins(self, load_catalog):
        os.environ["ICEBERG_CATALOG_URI"] = "http://explicit"
        os.environ["ATLAN_DOMAIN_NAME"] = "tenant.atlan.com"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        load_catalog.return_value = MagicMock()

        cat.load_catalog_from_env()

        kwargs = load_catalog.call_args.kwargs
        self.assertEqual(kwargs["uri"], "http://explicit")

    @patch("application_sdk.lakehouse._iceberg.catalog.load_catalog")
    def test_falls_back_to_domain(self, load_catalog):
        os.environ["ATLAN_DOMAIN_NAME"] = "tenant.atlan.com"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        load_catalog.return_value = MagicMock()

        cat.load_catalog_from_env()

        kwargs = load_catalog.call_args.kwargs
        self.assertEqual(
            kwargs["uri"], "https://tenant.atlan.com/api/polaris/api/catalog"
        )

    def test_raises_when_missing_client_id(self):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        with self.assertRaises(RuntimeError) as ctx:
            cat.load_catalog_from_env()
        self.assertIn("ICEBERG_CLIENT_ID", str(ctx.exception))

    def test_raises_when_missing_client_secret(self):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        with self.assertRaises(RuntimeError) as ctx:
            cat.load_catalog_from_env()
        self.assertIn("ICEBERG_CLIENT_SECRET", str(ctx.exception))

    @patch("application_sdk.lakehouse._iceberg.catalog.load_catalog")
    def test_defaults_to_context_store(self, load_catalog):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        load_catalog.return_value = MagicMock()
        cat.load_catalog_from_env()
        kwargs = load_catalog.call_args.kwargs
        self.assertEqual(kwargs["warehouse"], "context_store")

    @patch("application_sdk.lakehouse._iceberg.catalog.load_catalog")
    def test_custom_warehouse(self, load_catalog):
        os.environ["ICEBERG_CATALOG_URI"] = "http://x"
        os.environ["ICEBERG_CLIENT_ID"] = "id"
        os.environ["ICEBERG_CLIENT_SECRET"] = "secret"
        os.environ["ICEBERG_WAREHOUSE"] = "my_wh"
        load_catalog.return_value = MagicMock()
        cat.load_catalog_from_env()
        kwargs = load_catalog.call_args.kwargs
        self.assertEqual(kwargs["warehouse"], "my_wh")


class TestIdentifier(unittest.TestCase):
    def test_flat(self):
        self.assertEqual(cat.identifier("samples", "events"), ("samples", "events"))

    def test_dotted(self):
        self.assertEqual(
            cat.identifier("apps.databricks", "audit"),
            ("apps", "databricks", "audit"),
        )

    def test_strips_empty(self):
        self.assertEqual(
            cat.identifier(".apps..databricks.", "t"),
            ("apps", "databricks", "t"),
        )


class TestEnsureNamespace(unittest.TestCase):
    def test_swallows_already_exists(self):
        catalog = MagicMock()
        catalog.create_namespace.side_effect = Exception("exists")
        cat.ensure_namespace(catalog, "samples")
        catalog.create_namespace.assert_called_once_with(("samples",))

    def test_passes_tuple_for_dotted(self):
        catalog = MagicMock()
        cat.ensure_namespace(catalog, "apps.databricks")
        catalog.create_namespace.assert_called_once_with(("apps", "databricks"))


if __name__ == "__main__":
    unittest.main()
