"""Tests for SDK Schema → pyiceberg / pyarrow translation."""

import unittest

import pyarrow as pa
from pyiceberg.schema import Schema as IcebergSchema
from pyiceberg.transforms import BucketTransform, DayTransform, IdentityTransform
from pyiceberg.types import StringType, TimestampType

from application_sdk.lakehouse._iceberg import schema_mapper as mapper
from application_sdk.lakehouse.schema import Field, PartitionBy, Schema

_SCHEMA = Schema(
    fields=[
        Field("event_id", "string", nullable=False),
        Field("ingested_at", "timestamp", nullable=False),
        Field("payload", "string", nullable=True),
    ],
    partition_by=PartitionBy("ingested_at", transform="day"),
)


class TestToIcebergSchema(unittest.TestCase):
    def test_field_count_and_types(self):
        ice = mapper.to_iceberg_schema(_SCHEMA)
        self.assertIsInstance(ice, IcebergSchema)
        self.assertEqual(len(ice.fields), 3)
        self.assertIsInstance(ice.find_field("event_id").field_type, StringType)
        self.assertIsInstance(ice.find_field("ingested_at").field_type, TimestampType)

    def test_required_inverse_of_nullable(self):
        ice = mapper.to_iceberg_schema(_SCHEMA)
        self.assertTrue(ice.find_field("event_id").required)
        self.assertFalse(ice.find_field("payload").required)


class TestToArrowSchema(unittest.TestCase):
    def test_arrow_types(self):
        arrow_schema = mapper.to_arrow_schema(_SCHEMA)
        self.assertEqual(arrow_schema.field("event_id").type, pa.string())
        self.assertEqual(arrow_schema.field("ingested_at").type, pa.timestamp("us"))
        self.assertFalse(arrow_schema.field("event_id").nullable)
        self.assertTrue(arrow_schema.field("payload").nullable)


class TestPartitionSpec(unittest.TestCase):
    def test_unpartitioned(self):
        s = Schema(fields=[Field("x", "string")])
        ice = mapper.to_iceberg_schema(s)
        spec = mapper.to_partition_spec(s, ice)
        self.assertFalse(spec.fields)

    def test_identity(self):
        s = Schema(
            fields=[Field("status", "string", nullable=False)],
            partition_by=PartitionBy("status"),
        )
        ice = mapper.to_iceberg_schema(s)
        spec = mapper.to_partition_spec(s, ice)
        self.assertEqual(len(spec.fields), 1)
        self.assertIsInstance(spec.fields[0].transform, IdentityTransform)
        self.assertEqual(spec.fields[0].name, "status")

    def test_day(self):
        ice = mapper.to_iceberg_schema(_SCHEMA)
        spec = mapper.to_partition_spec(_SCHEMA, ice)
        self.assertEqual(len(spec.fields), 1)
        self.assertIsInstance(spec.fields[0].transform, DayTransform)
        self.assertEqual(spec.fields[0].name, "ingested_at_day")

    def test_bucket(self):
        s = Schema(
            fields=[Field("event_id", "string", nullable=False)],
            partition_by=PartitionBy("event_id", transform="bucket", bucket_count=8),
        )
        ice = mapper.to_iceberg_schema(s)
        spec = mapper.to_partition_spec(s, ice)
        self.assertIsInstance(spec.fields[0].transform, BucketTransform)
        self.assertEqual(spec.fields[0].name, "event_id_bucket_8")


if __name__ == "__main__":
    unittest.main()
