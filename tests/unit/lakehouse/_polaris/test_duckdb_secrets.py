"""Tests for per-cloud DuckDB secret builders."""

import os
import unittest
from unittest.mock import MagicMock

from application_sdk.lakehouse._polaris import duckdb_secrets as ds


class TestAwsS3Secret(unittest.TestCase):
    def setUp(self):
        os.environ.pop("AWS_REGION", None)

    def tearDown(self):
        os.environ.pop("AWS_REGION", None)

    def test_creates_credential_chain_secret_with_region(self):
        os.environ["AWS_REGION"] = "ap-south-1"
        con = MagicMock()
        ds.create_aws_s3_secret(con)
        sql = con.execute.call_args.args[0]
        self.assertIn("CREATE SECRET s3_secret", sql)
        self.assertIn("PROVIDER credential_chain", sql)
        self.assertIn("REGION 'ap-south-1'", sql)

    def test_empty_region_when_unset(self):
        con = MagicMock()
        ds.create_aws_s3_secret(con)
        sql = con.execute.call_args.args[0]
        self.assertIn("REGION ''", sql)


class TestGcpGcsSecret(unittest.TestCase):
    def test_creates_gcs_credential_chain_secret(self):
        con = MagicMock()
        ds.create_gcp_gcs_secret(con)
        sql = con.execute.call_args.args[0]
        self.assertIn("CREATE SECRET gcs_secret", sql)
        self.assertIn("TYPE gcs", sql)
        self.assertIn("PROVIDER credential_chain", sql)


class TestAzureSecretFromPolaris(unittest.TestCase):
    def _catalog_with_table_config(self, config: dict):
        catalog = MagicMock()
        catalog.list_tables.return_value = [("apps", "audit", "x")]
        table = MagicMock()
        table.config = config
        catalog.load_table.return_value = table
        return catalog

    def test_extracts_sas_and_creates_secret(self):
        catalog = self._catalog_with_table_config(
            {"adls.sas-token.myacct.dfs.core.windows.net": "sv=2024&sig=abc"}
        )
        con = MagicMock()
        ds.create_azure_secret_from_polaris(con, catalog, "apps.databricks")
        sqls = [c.args[0] for c in con.execute.call_args_list]
        self.assertTrue(any("INSTALL azure" in s for s in sqls))
        secret_sql = next(s for s in sqls if "CREATE SECRET azure_secret" in s)
        self.assertIn(
            "CONNECTION_STRING 'AccountName=myacct;SharedAccessSignature=sv=2024&sig=abc'",
            secret_sql,
        )

    def test_raises_when_namespace_empty(self):
        catalog = MagicMock()
        catalog.list_tables.return_value = []
        with self.assertRaises(ValueError) as ctx:
            ds.create_azure_secret_from_polaris(MagicMock(), catalog, "x")
        self.assertIn("No tables", str(ctx.exception))

    def test_raises_when_no_sas_in_table_config(self):
        catalog = self._catalog_with_table_config({"s3.access-key-id": "AKIA..."})
        with self.assertRaises(ValueError) as ctx:
            ds.create_azure_secret_from_polaris(MagicMock(), catalog, "x")
        self.assertIn("did not vend Azure SAS", str(ctx.exception))

    def test_rejects_malformed_account_name(self):
        # An uppercase / too-long / dotted account name from a malformed
        # Polaris response would be a SQL injection vector.
        catalog = self._catalog_with_table_config(
            {"adls.sas-token.BAD-ACCOUNT.dfs.core.windows.net": "sv=ok"}
        )
        with self.assertRaises(ValueError) as ctx:
            ds.create_azure_secret_from_polaris(MagicMock(), catalog, "x")
        self.assertIn("Invalid Azure account name", str(ctx.exception))

    def test_rejects_sas_with_quote(self):
        catalog = self._catalog_with_table_config(
            {"adls.sas-token.acct.dfs.core.windows.net": "sv=2024'; DROP TABLE x;--"}
        )
        with self.assertRaises(ValueError) as ctx:
            ds.create_azure_secret_from_polaris(MagicMock(), catalog, "x")
        self.assertIn("disallowed characters", str(ctx.exception))

    def test_rejects_sas_with_semicolon(self):
        catalog = self._catalog_with_table_config(
            {"adls.sas-token.acct.dfs.core.windows.net": "sv=2024;evil=1"}
        )
        with self.assertRaises(ValueError):
            ds.create_azure_secret_from_polaris(MagicMock(), catalog, "x")


if __name__ == "__main__":
    unittest.main()
