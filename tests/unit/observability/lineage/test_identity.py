"""Golden tests for the canonical ARS identity hash (the connector<->publish join key).

These guard the join-break surfaces called out in the red-team report: key-order,
case-folding, null omission, and version stability.
"""

from application_sdk.observability.lineage import (
    IDENTITY_SCHEMA_VERSION,
    canonical_identity_string,
    components_hash,
    stitch_key,
)


def _components(**overrides):
    base = {
        "connectorType": "snowflake",
        "databaseName": "ANALYTICS",
        "schemaName": "PUBLIC",
        "tableName": "ORDERS",
        "columnName": "AMOUNT",
    }
    base.update(overrides)
    return base


def test_hash_is_deterministic():
    assert components_hash(_components()) == components_hash(_components())


def test_hash_is_key_order_invariant():
    a = {
        "connectorType": "snowflake",
        "databaseName": "ANALYTICS",
        "schemaName": "PUBLIC",
        "tableName": "ORDERS",
    }
    b = {
        "tableName": "ORDERS",
        "schemaName": "PUBLIC",
        "databaseName": "ANALYTICS",
        "connectorType": "snowflake",
    }
    assert components_hash(a) == components_hash(b)


def test_identity_fields_are_case_normalized():
    # database/schema/table/column upper-cased; connectorType lower-cased.
    lower = _components(
        connectorType="SNOWFLAKE",
        databaseName="analytics",
        schemaName="public",
        tableName="orders",
        columnName="amount",
    )
    assert components_hash(lower) == components_hash(_components())


def test_null_and_empty_are_omitted_consistently():
    with_nulls = _components(columnName=None)
    without_key = {k: v for k, v in _components().items() if k != "columnName"}
    with_empty = _components(columnName="")
    assert (
        components_hash(with_nulls)
        == components_hash(without_key)
        == components_hash(with_empty)
    )


def test_table_vs_column_grain_differ():
    table_grain = {k: v for k, v in _components().items() if k != "columnName"}
    assert components_hash(table_grain) != components_hash(_components())


def test_canonical_string_shape():
    s = canonical_identity_string(_components())
    assert s == "snowflake|ANALYTICS|PUBLIC|ORDERS|AMOUNT"


def test_empty_components_does_not_raise():
    assert components_hash(None) == components_hash({})


def test_stitch_key_composition():
    key = stitch_key("default/tableau/site/ds-1", "inputs", 0, _components())
    assert key.startswith("default/tableau/site/ds-1|inputs|0|")
    assert key.endswith(components_hash(_components()))


def test_identity_schema_version_is_int():
    assert isinstance(IDENTITY_SCHEMA_VERSION, int) and IDENTITY_SCHEMA_VERSION >= 1
