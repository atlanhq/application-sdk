"""Tests for application_sdk.common.asset_serialization (FND-2056).

The regression these pin: a ``pyatlan_v9`` asset exposes none of
``to_nested_dict`` / ``model_dump`` / ``dict``, so the probe chain this seam
replaced fell through to writing the *raw source record*. The run then
published unmapped source rows as entities and reported SUCCESS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import orjson
import pytest
from pyatlan_v9.model.assets import Table

from application_sdk.common.asset_serialization import (
    ModelDumpAsset,
    NestedBytesAsset,
    NestedDictAsset,
    entity_bytes,
    orjson_default,
)
from application_sdk.common.errors import UnserializableMapperResultError

SCHEMA_QN = "default/mysql/1234567890/db/sch"


def _table(name: str = "T1") -> Table:
    return Table.creator(name=name, schema_qualified_name=SCHEMA_QN)


@dataclass
class NestedDictOnly:
    """An asset shape that renders itself as an Atlas nested dict."""

    payload: dict[str, Any]
    connection_name: str | None = None

    def to_nested_dict(self) -> dict[str, Any]:
        return self.payload


@dataclass
class ModelDumpOnly:
    """A pydantic-style asset shape exposing only ``model_dump()``."""

    payload: dict[str, Any]
    connection_name: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return self.payload


@dataclass
class NotAnAsset:
    """The shape the deleted fallback used to swallow."""

    name: str


class TestPyatlanV9Asset:
    """to_nested_bytes is the only serialiser a pyatlan_v9 asset has."""

    def test_asset_serialises_to_atlas_wire_shape(self):
        out = orjson.loads(entity_bytes(_table(), entity_type="table"))

        assert out["typeName"] == "Table"
        assert out["attributes"]["qualifiedName"] == f"{SCHEMA_QN}/T1"

    def test_asset_exposes_none_of_the_old_probe_names(self):
        """The regression: the old probe chain wrote ``record`` for this type."""
        asset = _table()

        assert not hasattr(asset, "to_nested_dict")
        assert not hasattr(asset, "model_dump")
        assert not isinstance(asset, dict)
        assert isinstance(asset, NestedBytesAsset)

    def test_bytes_are_jsonl_safe(self):
        """No raw newline, so the caller can write the result as one JSONL line."""
        asset = _table(name="weird\nname\twith control chars")

        assert b"\n" not in entity_bytes(asset)

    def test_no_round_trip_through_a_dict(self):
        """The asset's own encoder output is passed through byte-for-byte."""
        asset = _table()

        assert entity_bytes(asset) == asset.to_nested_bytes()


class TestConnectionNameInjection:
    def test_injected_onto_an_asset_that_left_it_unset(self):
        out = orjson.loads(entity_bytes(_table(), connection_name="my-conn"))

        assert out["attributes"]["connectionName"] == "my-conn"

    def test_mapper_set_value_wins(self):
        asset = _table()
        asset.connection_name = "mapper-chose-this"

        out = orjson.loads(entity_bytes(asset, connection_name="sdk-default"))

        assert out["attributes"]["connectionName"] == "mapper-chose-this"

    def test_empty_connection_name_injects_nothing(self):
        out = orjson.loads(entity_bytes(_table(), connection_name=""))

        assert "connectionName" not in out["attributes"]

    def test_injected_into_a_dict_under_attributes(self):
        out = orjson.loads(
            entity_bytes({"typeName": "Table"}, connection_name="my-conn")
        )

        assert out["attributes"]["connectionName"] == "my-conn"

    def test_existing_dict_value_wins(self):
        payload = {"typeName": "Table", "attributes": {"connectionName": "theirs"}}

        out = orjson.loads(entity_bytes(payload, connection_name="ours"))

        assert out["attributes"]["connectionName"] == "theirs"

    def test_non_dict_attributes_left_alone(self):
        """A malformed ``attributes`` is not something the injector may rewrite."""
        payload: dict[str, Any] = {"typeName": "Table", "attributes": "not-a-dict"}

        out = orjson.loads(entity_bytes(payload, connection_name="ours"))

        assert out["attributes"] == "not-a-dict"

    def test_object_without_the_attribute_is_untouched(self):
        asset = NestedDictOnly(payload={"typeName": "Custom"})
        del asset.connection_name

        assert orjson.loads(entity_bytes(asset, connection_name="c")) == {
            "typeName": "Custom"
        }


class TestDispatchOrder:
    def test_nested_dict_shape(self):
        asset = NestedDictOnly(payload={"typeName": "Custom", "attributes": {}})

        assert orjson.loads(entity_bytes(asset))["typeName"] == "Custom"
        assert isinstance(asset, NestedDictAsset)

    def test_model_dump_shape(self):
        asset = ModelDumpOnly(payload={"typeName": "Legacy", "attributes": {}})

        assert orjson.loads(entity_bytes(asset))["typeName"] == "Legacy"
        assert isinstance(asset, ModelDumpAsset)

    def test_nested_encoders_beat_model_dump(self):
        """model_dump yields field names, not the wire shape — it loses ties."""

        @dataclass
        class Both:
            connection_name: str | None = None

            def to_nested_dict(self) -> dict[str, Any]:
                return {"source": "to_nested_dict"}

            def model_dump(self) -> dict[str, Any]:
                return {"source": "model_dump"}

        assert orjson.loads(entity_bytes(Both()))["source"] == "to_nested_dict"

    def test_plain_dict_passthrough(self):
        payload = {"typeName": "Table", "attributes": {"name": "t"}}

        assert orjson.loads(entity_bytes(payload)) == payload


class TestUnserializableResult:
    def test_unknown_type_raises(self):
        with pytest.raises(UnserializableMapperResultError):
            entity_bytes(NotAnAsset(name="x"), entity_type="table")

    def test_error_names_the_type_and_the_entity(self):
        with pytest.raises(UnserializableMapperResultError) as exc:
            entity_bytes(NotAnAsset(name="x"), entity_type="column")

        assert exc.value.observed == "NotAnAsset"
        assert exc.value.location == "column"

    def test_none_raises_rather_than_writing_null(self):
        with pytest.raises(UnserializableMapperResultError):
            entity_bytes(None, entity_type="table")

    def test_a_returned_string_raises(self):
        """A mapper that returned pre-serialised JSON is still a contract breach."""
        with pytest.raises(UnserializableMapperResultError):
            entity_bytes('{"typeName": "Table"}', entity_type="table")

    def test_error_is_not_retryable(self):
        with pytest.raises(UnserializableMapperResultError) as exc:
            entity_bytes(NotAnAsset(name="x"))

        assert exc.value.effective_retryable is False


class TestOrjsonDefault:
    def test_decimal_becomes_float(self):
        assert orjson_default(Decimal("1.5")) == 1.5

    def test_bytes_become_text(self):
        assert orjson_default(b"hello") == "hello"

    def test_undecodable_bytes_are_replaced_not_raised(self):
        assert orjson_default(b"\xff") == "�"

    def test_unknown_type_raises_typeerror_for_the_orjson_protocol(self):
        with pytest.raises(TypeError):
            orjson_default(NotAnAsset(name="x"))

    def test_dict_branch_uses_the_default(self):
        payload = {"attributes": {"rows": Decimal("42"), "blob": b"bin"}}

        out = orjson.loads(entity_bytes(payload))

        assert out["attributes"] == {"rows": 42.0, "blob": "bin"}

    def test_datetime_needs_no_default(self):
        stamp = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

        out = orjson.loads(entity_bytes({"attributes": {"created": stamp}}))

        assert out["attributes"]["created"].startswith("2026-09-15T12:00:00")
