"""Ref identity hash v2: the join key between an emitted ref and its lookup outcome."""

from __future__ import annotations

import hashlib

from application_sdk.observability.lineage import (
    IDENTITY_SCHEMA_VERSION,
    components_hash,
)
from application_sdk.observability.lineage.identity import canonical_identity_string

TABLE_REF = {
    "connectorType": "Snowflake",
    "databaseName": "analytics",
    "schemaName": "core",
    "name": "orders",
}


def test_version_is_two():
    assert IDENTITY_SCHEMA_VERSION == 2


def test_canonical_string_and_case_policy():
    assert canonical_identity_string(TABLE_REF) == "snowflake|ANALYTICS|CORE||ORDERS"


def test_hash_is_sha256_of_canonical_string():
    expected = hashlib.sha256(b"snowflake|ANALYTICS|CORE||ORDERS").hexdigest()
    assert components_hash(TABLE_REF) == expected


def test_key_order_and_whitespace_do_not_matter():
    shuffled = {
        "name": " orders ",
        "schemaName": "CORE",
        "databaseName": "Analytics",
        "connectorType": "snowflake",
    }
    assert components_hash(shuffled) == components_hash(TABLE_REF)


def test_tables_in_one_schema_hash_differently():
    other = {**TABLE_REF, "name": "customers"}
    assert components_hash(other) != components_hash(TABLE_REF)


def test_column_name_falls_back_into_name():
    by_name = {**TABLE_REF, "tableName": "orders", "name": "id"}
    by_column = {**TABLE_REF, "tableName": "orders", "columnName": "id"}
    del by_column["name"]
    assert components_hash(by_name) == components_hash(by_column)


def test_none_and_blank_are_empty():
    assert canonical_identity_string({}) == "||||"
    assert canonical_identity_string({"name": "  ", "columnName": None}) == "||||"
    assert components_hash(None) == components_hash({})
