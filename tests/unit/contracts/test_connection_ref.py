"""Unit tests for ConnectionRef and ConnectionAttributes.

Covers:
- Alias deserialization from AE wire format (camelCase → snake_case)
- model_dump(by_alias=True) produces the correct AE wire shape
- from_connection(): pyatlan_v9 flat struct → ConnectionRef via to_atlas_format
- to_connection(): ConnectionRef → pyatlan_v9 flat struct via from_atlas_format
- Round-trip: from_connection(conn).to_connection() preserves field values
- extra="allow" on ConnectionRef preserves unknown top-level AE fields
- populate_by_name=True accepts both snake_case and camelCase on construction
"""

from __future__ import annotations

import msgspec
import pytest
from pyatlan_v9.model.transform import get_type

from application_sdk.contracts.types import ConnectionAttributes, ConnectionRef


def _make_conn(
    qualified_name: str = "default/snowflake/123",
    name: str = "my-conn",
    admin_users: list[str] | None = None,
    admin_roles: list[str] | None = None,
    admin_groups: list[str] | None = None,
) -> object:
    """Build a pyatlan_v9 Connection struct with camelCase keys."""
    cls = get_type("Connection")
    return msgspec.convert(
        {
            "typeName": "Connection",
            "qualifiedName": qualified_name,
            "name": name,
            "adminUsers": admin_users or [],
            "adminRoles": admin_roles or [],
            "adminGroups": admin_groups or [],
        },
        cls,
        strict=False,
    )


# ---------------------------------------------------------------------------
# ConnectionAttributes
# ---------------------------------------------------------------------------


class TestConnectionAttributes:
    def test_alias_deserialization(self) -> None:
        attrs = ConnectionAttributes.model_validate(
            {"qualifiedName": "default/sf/1", "adminUsers": ["alice"]}
        )
        assert attrs.qualified_name == "default/sf/1"
        assert attrs.admin_users == ["alice"]

    def test_snake_case_construction(self) -> None:
        attrs = ConnectionAttributes(
            qualified_name="default/sf/1",
            admin_users=["alice"],
        )
        assert attrs.qualified_name == "default/sf/1"

    def test_dump_by_alias_produces_camel_case(self) -> None:
        attrs = ConnectionAttributes(
            qualified_name="default/sf/1",
            admin_users=["alice"],
            admin_roles=["r1"],
            admin_groups=["g1"],
        )
        dumped = attrs.model_dump(by_alias=True)
        assert dumped["qualifiedName"] == "default/sf/1"
        assert dumped["adminUsers"] == ["alice"]
        assert dumped["adminRoles"] == ["r1"]
        assert dumped["adminGroups"] == ["g1"]
        assert "qualified_name" not in dumped

    def test_extra_fields_allowed(self) -> None:
        attrs = ConnectionAttributes.model_validate(
            {"qualifiedName": "x", "unknownField": "preserved"}
        )
        assert attrs.model_dump(by_alias=True)["unknownField"] == "preserved"

    def test_is_frozen(self) -> None:
        from pydantic import ValidationError

        attrs = ConnectionAttributes(qualified_name="x")
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            attrs.qualified_name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ConnectionRef — construction and serialization
# ---------------------------------------------------------------------------


class TestConnectionRefConstruction:
    def test_alias_deserialization_from_ae_wire(self) -> None:
        ref = ConnectionRef.model_validate(
            {
                "typeName": "Connection",
                "attributes": {
                    "qualifiedName": "default/snowflake/123",
                    "name": "sf-conn",
                    "adminUsers": ["alice", "bob"],
                },
            }
        )
        assert ref.type_name == "Connection"
        assert ref.attributes.qualified_name == "default/snowflake/123"
        assert ref.attributes.name == "sf-conn"
        assert ref.attributes.admin_users == ["alice", "bob"]

    def test_snake_case_construction(self) -> None:
        ref = ConnectionRef(
            type_name="Connection",
            attributes=ConnectionAttributes(qualified_name="default/sf/1"),
        )
        assert ref.type_name == "Connection"
        assert ref.attributes.qualified_name == "default/sf/1"

    def test_camel_case_construction_via_populate_by_name(self) -> None:
        # populate_by_name=True means both snake_case and camelCase work
        ref_snake = ConnectionRef(type_name="Connection")
        ref_camel = ConnectionRef.model_validate({"typeName": "Connection"})
        assert ref_snake.type_name == ref_camel.type_name == "Connection"

    def test_dump_by_alias_produces_ae_wire_shape(self) -> None:
        ref = ConnectionRef(
            type_name="Connection",
            attributes=ConnectionAttributes(
                qualified_name="default/sf/1",
                name="sf",
                admin_users=["alice"],
                admin_roles=["r1"],
                admin_groups=["g1"],
            ),
        )
        dumped = ref.model_dump(by_alias=True)
        assert dumped["typeName"] == "Connection"
        assert "typeName" in dumped
        assert "type_name" not in dumped
        attrs = dumped["attributes"]
        assert attrs["qualifiedName"] == "default/sf/1"
        assert attrs["adminUsers"] == ["alice"]
        assert attrs["adminRoles"] == ["r1"]
        assert attrs["adminGroups"] == ["g1"]

    def test_top_level_extra_fields_preserved(self) -> None:
        ref = ConnectionRef.model_validate(
            {
                "typeName": "Connection",
                "attributes": {},
                "guid": "some-guid-value",
            }
        )
        dumped = ref.model_dump(by_alias=True)
        assert dumped.get("guid") == "some-guid-value"

    def test_is_frozen(self) -> None:
        from pydantic import ValidationError

        ref = ConnectionRef(type_name="Connection")
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            ref.type_name = "Other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# from_connection
# ---------------------------------------------------------------------------


class TestFromConnection:
    def test_qualified_name_populated(self) -> None:
        conn = _make_conn(qualified_name="default/sf/123")
        ref = ConnectionRef.from_connection(conn)
        assert ref.attributes.qualified_name == "default/sf/123"

    def test_name_populated(self) -> None:
        conn = _make_conn(name="my-snowflake")
        ref = ConnectionRef.from_connection(conn)
        assert ref.attributes.name == "my-snowflake"

    def test_admin_lists_populated(self) -> None:
        conn = _make_conn(
            admin_users=["alice", "bob"],
            admin_roles=["role1"],
            admin_groups=["grp1"],
        )
        ref = ConnectionRef.from_connection(conn)
        assert set(ref.attributes.admin_users) == {"alice", "bob"}
        assert "role1" in ref.attributes.admin_roles
        assert "grp1" in ref.attributes.admin_groups

    def test_type_name_is_connection(self) -> None:
        conn = _make_conn()
        ref = ConnectionRef.from_connection(conn)
        assert ref.type_name == "Connection"

    def test_returns_connection_ref_instance(self) -> None:
        conn = _make_conn()
        ref = ConnectionRef.from_connection(conn)
        assert isinstance(ref, ConnectionRef)
        assert isinstance(ref.attributes, ConnectionAttributes)


# ---------------------------------------------------------------------------
# to_connection
# ---------------------------------------------------------------------------


class TestToConnection:
    def test_qualified_name_preserved(self) -> None:
        ref = ConnectionRef(
            type_name="Connection",
            attributes=ConnectionAttributes(qualified_name="default/sf/1"),
        )
        conn = ref.to_connection()
        assert conn.qualified_name == "default/sf/1"

    def test_name_preserved(self) -> None:
        ref = ConnectionRef(
            type_name="Connection",
            attributes=ConnectionAttributes(name="sf-conn"),
        )
        conn = ref.to_connection()
        assert conn.name == "sf-conn"

    def test_admin_users_preserved(self) -> None:
        ref = ConnectionRef(
            type_name="Connection",
            attributes=ConnectionAttributes(admin_users=["alice", "bob"]),
        )
        conn = ref.to_connection()
        assert set(conn.admin_users) == {"alice", "bob"}

    def test_returns_asset_instance(self) -> None:
        from pyatlan_v9.model.assets.asset import Asset

        ref = ConnectionRef(type_name="Connection")
        conn = ref.to_connection()
        assert isinstance(conn, Asset)


# ---------------------------------------------------------------------------
# Round-trip: from_connection → to_connection
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_full_round_trip_preserves_fields(self) -> None:
        original = _make_conn(
            qualified_name="default/snowflake/456",
            name="prod-conn",
            admin_users=["alice"],
            admin_roles=["admin"],
            admin_groups=["data-team"],
        )
        ref = ConnectionRef.from_connection(original)
        restored = ref.to_connection()

        assert restored.qualified_name == original.qualified_name
        assert restored.name == original.name

    def test_ae_wire_round_trip(self) -> None:
        wire = {
            "typeName": "Connection",
            "attributes": {
                "qualifiedName": "default/sf/789",
                "name": "staging",
                "adminUsers": ["charlie"],
            },
        }
        ref = ConnectionRef.model_validate(wire)
        dumped = ref.model_dump(by_alias=True)

        assert dumped["typeName"] == "Connection"
        assert dumped["attributes"]["qualifiedName"] == "default/sf/789"
        assert dumped["attributes"]["name"] == "staging"
        assert dumped["attributes"]["adminUsers"] == ["charlie"]
