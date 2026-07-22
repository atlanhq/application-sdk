"""Public tests for LakehouseWriter: dict primitives + SDK Schema."""

import logging
import unittest
from unittest.mock import MagicMock, patch

from application_sdk.lakehouse import Field, LakehouseWriter, Schema

_AUDIT_SCHEMA = Schema(
    fields=[
        Field("event_id", "string", nullable=False),
        Field("payload", "string", nullable=True),
    ]
)


class TestLakehouseWriter(unittest.TestCase):
    def test_app_namespace_property(self):
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        self.assertEqual(writer.app_namespace, "apps.databricks")

    @patch("application_sdk.lakehouse.writer._ops.append_records")
    def test_append_skips_empty(self, append):
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        rows = writer.append("audit", [])
        self.assertEqual(rows, 0)
        append.assert_not_called()

    @patch("application_sdk.lakehouse.writer._ops.append_records")
    def test_append_delegates_with_schema(self, append):
        append.return_value = 1
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        rows = writer.append(
            "audit", [{"event_id": "e1", "payload": "x"}], schema=_AUDIT_SCHEMA
        )
        self.assertEqual(rows, 1)
        args, kwargs = append.call_args
        # positional: (catalog, namespace, table, records)
        self.assertEqual(args[1], "apps.databricks")
        self.assertEqual(args[2], "audit")
        self.assertEqual(args[3], [{"event_id": "e1", "payload": "x"}])
        self.assertIs(kwargs["schema"], _AUDIT_SCHEMA)

    @patch("application_sdk.lakehouse.writer._ops.append_records")
    def test_cross_namespace_warns_but_proceeds(self, append):
        append.return_value = 1
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        with self.assertLogs(
            "application_sdk.lakehouse.writer", level="WARNING"
        ) as captured:
            writer.append(
                "evil",
                [{"event_id": "e1"}],
                namespace="other_app",
                schema=_AUDIT_SCHEMA,
            )
        self.assertTrue(
            any("Cross-namespace write" in m for m in captured.output), captured.output
        )
        # And the call still happened with the cross-ns target
        args, _ = append.call_args
        self.assertEqual(args[1], "other_app")

    @patch("application_sdk.lakehouse.writer._ops.ensure_table")
    def test_ensure_table_delegates(self, ensure):
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        writer.ensure_table("audit", _AUDIT_SCHEMA)
        ensure.assert_called_once()
        args, _ = ensure.call_args
        self.assertEqual(args[1], "apps.databricks")
        self.assertEqual(args[2], "audit")
        self.assertIs(args[3], _AUDIT_SCHEMA)


class TestLakehouseWriterAppendBulk(unittest.TestCase):
    @patch("application_sdk.lakehouse._daft.writer.write_bulk")
    def test_append_bulk_delegates_to_daft_writer(self, write_bulk):
        write_bulk.return_value = 100
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        rows = writer.append_bulk(
            "metrics",
            "local/staging/run-001/",
            schema=_AUDIT_SCHEMA,
            mode="overwrite",
        )
        self.assertEqual(rows, 100)
        args, kwargs = write_bulk.call_args
        # positional: (catalog, namespace, table, source_prefix)
        self.assertEqual(args[1], "apps.databricks")
        self.assertEqual(args[2], "metrics")
        self.assertEqual(args[3], "local/staging/run-001/")
        self.assertEqual(kwargs["mode"], "overwrite")
        self.assertIs(kwargs["schema"], _AUDIT_SCHEMA)

    @patch("application_sdk.lakehouse._daft.writer.write_bulk")
    def test_append_bulk_default_mode_is_append(self, write_bulk):
        write_bulk.return_value = 1
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        writer.append_bulk("t", "prefix/")
        self.assertEqual(write_bulk.call_args.kwargs["mode"], "append")

    @patch("application_sdk.lakehouse._daft.writer.write_bulk")
    def test_append_bulk_cross_namespace_warns(self, write_bulk):
        write_bulk.return_value = 1
        writer = LakehouseWriter(MagicMock(), app_namespace="apps.databricks")
        with self.assertLogs(
            "application_sdk.lakehouse.writer", level="WARNING"
        ) as captured:
            writer.append_bulk("t", "prefix/", namespace="other_app")
        self.assertTrue(any("Cross-namespace write" in m for m in captured.output))


class TestLakehouseWriterFromEnv(unittest.TestCase):
    @patch("application_sdk.lakehouse.writer._catalog.load_catalog_from_env")
    def test_from_env_binds_namespace(self, loader):
        loader.return_value = MagicMock()
        writer = LakehouseWriter.from_env(app_namespace="apps.databricks")
        self.assertEqual(writer.app_namespace, "apps.databricks")


class TestCrossNamespaceWarningContent(unittest.TestCase):
    def test_warning_includes_both_namespaces(self):
        writer = LakehouseWriter(MagicMock(), app_namespace="my_app")
        logger = logging.getLogger("application_sdk.lakehouse.writer")
        with self.assertLogs(logger, level="WARNING") as captured:
            writer._check_namespace("not_my_app")
        msg = captured.output[0]
        self.assertIn("my_app", msg)
        self.assertIn("not_my_app", msg)


if __name__ == "__main__":
    unittest.main()
