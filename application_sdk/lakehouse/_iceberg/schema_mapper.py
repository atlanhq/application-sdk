"""Internal: map SDK :class:`Schema` to pyiceberg + pyarrow types.

Apps declare schemas in SDK terms (``Schema(fields=[Field("x", "string")])``).
This module translates each side when the writer needs to create an Iceberg
table or build an Arrow batch from a list of dict records.
"""

from __future__ import annotations

import pyarrow as pa
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema as IcebergSchema
from pyiceberg.transforms import (
    BucketTransform,
    DayTransform,
    HourTransform,
    IdentityTransform,
    MonthTransform,
    Transform,
    YearTransform,
)
from pyiceberg.types import (
    BooleanType,
    DateType,
    DoubleType,
    IntegerType,
    LongType,
    NestedField,
    StringType,
    TimestampType,
)

from application_sdk.lakehouse.schema import PartitionBy, Schema

# SDK type → pyiceberg type
_ICEBERG_TYPE = {
    "string": StringType(),
    "int": IntegerType(),
    "long": LongType(),
    "double": DoubleType(),
    "boolean": BooleanType(),
    "timestamp": TimestampType(),  # naive UTC, microsecond
    "date": DateType(),
}

# SDK type → pyarrow type
_ARROW_TYPE = {
    "string": pa.string(),
    "int": pa.int32(),
    "long": pa.int64(),
    "double": pa.float64(),
    "boolean": pa.bool_(),
    "timestamp": pa.timestamp("us"),
    "date": pa.date32(),
}


def to_iceberg_schema(schema: Schema) -> IcebergSchema:
    """Translate the SDK :class:`Schema` to a pyiceberg :class:`Schema`."""
    nested_fields = [
        NestedField(
            field_id=i + 1,
            name=field.name,
            field_type=_ICEBERG_TYPE[field.type],
            required=not field.nullable,
        )
        for i, field in enumerate(schema.fields)
    ]
    return IcebergSchema(*nested_fields)


def to_arrow_schema(schema: Schema) -> pa.Schema:
    """Translate the SDK :class:`Schema` to a pyarrow :class:`Schema`."""
    return pa.schema(
        [
            pa.field(field.name, _ARROW_TYPE[field.type], nullable=field.nullable)
            for field in schema.fields
        ]
    )


def to_partition_spec(schema: Schema, iceberg_schema: IcebergSchema) -> PartitionSpec:
    """Translate ``schema.partition_by`` to a pyiceberg :class:`PartitionSpec`.

    Returns the unpartitioned spec if no partitioning is declared.
    """
    specs = schema.partition_specs()
    if not specs:
        return PartitionSpec()
    fields = []
    for index, spec in enumerate(specs):
        source = iceberg_schema.find_field(spec.column)
        fields.append(
            PartitionField(
                source_id=source.field_id,
                field_id=1000 + index,
                transform=_iceberg_transform(spec),
                name=_partition_field_name(spec),
            )
        )
    return PartitionSpec(*fields)


def _iceberg_transform(spec: PartitionBy) -> Transform:
    if spec.transform == "identity":
        return IdentityTransform()
    if spec.transform == "year":
        return YearTransform()
    if spec.transform == "month":
        return MonthTransform()
    if spec.transform == "day":
        return DayTransform()
    if spec.transform == "hour":
        return HourTransform()
    if spec.transform == "bucket":
        assert spec.bucket_count is not None  # validated in PartitionBy.__post_init__
        return BucketTransform(num_buckets=spec.bucket_count)
    raise ValueError(f"Unsupported partition transform: {spec.transform!r}")


def _partition_field_name(spec: PartitionBy) -> str:
    if spec.transform == "identity":
        return spec.column
    if spec.transform == "bucket":
        return f"{spec.column}_bucket_{spec.bucket_count}"
    return f"{spec.column}_{spec.transform}"


def field_names(schema: Schema) -> list[str]:
    return [f.name for f in schema.fields]
