"""Regression test pinning the transformer to duckdb API valid across our range.

The ``[sql]`` / ``[incremental]`` extras allow ``duckdb>=1.1.3,<1.6.0``, but the
lockfile CI runs against resolves the newest release in that window. Anything
duckdb added late in the range therefore looks available in CI while breaking
every consumer resolving lower — which is how
``DuckDBPyConnection.to_arrow_table`` (added in duckdb 1.5.0) reached a release.

This test hides the connection methods that are not valid across the whole
declared range and drives the real transform, so the bounds are exercised on
whatever duckdb is installed.
"""

import json
import os
from typing import Any

import pyarrow as pa
import pytest

from application_sdk.common.incremental.storage import duckdb_utils
from application_sdk.transformers.query import QueryBasedTransformer

# Connection methods usable on only part of duckdb>=1.1.3,<1.6.0.
CONNECTION_METHODS_OUTSIDE_RANGE = {
    "to_arrow_table": "added in duckdb 1.5.0",
    "fetch_arrow_table": "deprecated in duckdb 1.5.0",
}


class DeclaredRangeConnection:
    """A duckdb connection exposing only API valid on every allowed duckdb."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def __getattr__(self, name: str) -> Any:
        reason = CONNECTION_METHODS_OUTSIDE_RANGE.get(name)
        if reason:
            raise AttributeError(f"DuckDBPyConnection.{name} is {reason}")
        attribute = getattr(self._connection, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attribute(*args, **kwargs)
            # execute() returns the connection itself; keep it restricted.
            return self if result is self._connection else result

        return call


@pytest.fixture
def duckdb_within_declared_range(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connection = duckdb_utils.DuckDBConnectionManager.connection.fget
    monkeypatch.setattr(
        duckdb_utils.DuckDBConnectionManager,
        "connection",
        property(lambda self: DeclaredRangeConnection(real_connection(self))),
    )


def test_transform_metadata_uses_only_range_safe_duckdb_api(
    duckdb_within_declared_range: None,
    postgres_transformer: QueryBasedTransformer,
    transform_args: dict[str, Any],
) -> None:
    resource = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "resources/raw/database.json"
    )
    with open(resource, "r") as raw:
        records = [json.loads(line) for line in raw if line.strip()]

    result = postgres_transformer.transform_metadata(
        "DATABASE", pa.Table.from_pylist(records), **transform_args
    )

    assert result is not None
    assert len(result) == len(records)
