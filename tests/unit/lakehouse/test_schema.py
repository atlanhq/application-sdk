"""Tests for the SDK Schema / Field / PartitionBy dataclasses."""

import unittest

from application_sdk.lakehouse.schema import Field, PartitionBy, Schema


class TestField(unittest.TestCase):
    def test_defaults(self):
        f = Field("x", "string")
        self.assertEqual(f.name, "x")
        self.assertEqual(f.type, "string")
        self.assertTrue(f.nullable)


class TestPartitionBy(unittest.TestCase):
    def test_identity_default(self):
        p = PartitionBy("status")
        self.assertEqual(p.transform, "identity")
        self.assertIsNone(p.bucket_count)

    def test_bucket_requires_count(self):
        with self.assertRaises(ValueError):
            PartitionBy("event_id", transform="bucket")

    def test_bucket_with_count(self):
        p = PartitionBy("event_id", transform="bucket", bucket_count=16)
        self.assertEqual(p.bucket_count, 16)


class TestSchema(unittest.TestCase):
    def test_empty_fields_rejected(self):
        with self.assertRaises(ValueError):
            Schema(fields=[])

    def test_duplicate_field_names_rejected(self):
        with self.assertRaises(ValueError):
            Schema(fields=[Field("x", "string"), Field("x", "int")])

    def test_partition_by_unknown_column_rejected(self):
        with self.assertRaises(ValueError):
            Schema(
                fields=[Field("x", "string")],
                partition_by=PartitionBy("not_a_field"),
            )

    def test_partition_specs_handles_single(self):
        s = Schema(
            fields=[Field("x", "string")],
            partition_by=PartitionBy("x"),
        )
        self.assertEqual(len(s.partition_specs()), 1)

    def test_partition_specs_handles_list(self):
        s = Schema(
            fields=[Field("x", "string"), Field("y", "string")],
            partition_by=[PartitionBy("x"), PartitionBy("y")],
        )
        self.assertEqual(len(s.partition_specs()), 2)

    def test_partition_specs_empty_when_none(self):
        s = Schema(fields=[Field("x", "string")])
        self.assertEqual(s.partition_specs(), [])

    def test_field_names(self):
        s = Schema(fields=[Field("a", "string"), Field("b", "int")])
        self.assertEqual(s.field_names(), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
