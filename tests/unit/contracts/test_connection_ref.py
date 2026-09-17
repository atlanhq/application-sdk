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

from typing import Any

import msgspec
import orjson
import pytest
from pyatlan_v9.model.transform import get_type, to_atlas_format
from pydantic import ValidationError

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

    def test_connector_name_and_category(self) -> None:
        attrs = ConnectionAttributes.model_validate(
            {
                "qualifiedName": "x",
                "connectorName": "snowflake",
                "category": "warehouse",
            }
        )
        assert attrs.connector_name == "snowflake"
        assert attrs.category == "warehouse"
        dumped = attrs.model_dump(by_alias=True)
        assert dumped["connectorName"] == "snowflake"
        assert dumped["category"] == "warehouse"

    def test_connector_name_and_category_default_none(self) -> None:
        attrs = ConnectionAttributes(qualified_name="x")
        assert attrs.connector_name is None
        assert attrs.category is None

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


class TestNullAdminListsAreAValue:
    """A cleared ``admin_*`` list survives; a null identity does not.

    Only observable from pyatlan 11.3.0. Up to 11.2.0 ``to_atlas_format``
    dropped explicit nulls, so a cleared field reached
    :meth:`ConnectionRef.from_connection` indistinguishable from one that was
    never set — see ``TestPyatlanFlattenContract`` in
    ``tests/unit/common/test_entity_envelope.py``.

    The two halves are deliberately asymmetric. Clearing ``admin_roles`` while
    granting ``admin_groups`` is an ordinary ACL edit and Atlas accepts
    clearing all three, so a null there is a value to carry. ``qualifiedName``
    and ``name`` are identity: coercing a null to ``""`` would manufacture an
    empty identity and surface the failure somewhere further from its cause.
    """

    def test_from_connection_preserves_a_cleared_admin_list(self) -> None:
        conn = _make_conn(admin_users=["alice"])
        conn.admin_roles = None  # type: ignore[attr-defined]

        ref = ConnectionRef.from_connection(conn)

        assert ref.attributes.admin_roles is None
        assert set(ref.attributes.admin_users or []) == {"alice"}

    def test_an_absent_admin_list_is_an_empty_list_not_none(self) -> None:
        """Absent and cleared must not collapse onto each other."""
        ref = ConnectionRef.model_validate(
            {"typeName": "Connection", "attributes": {"name": "c"}}
        )

        assert ref.attributes.admin_roles == []
        assert ref.attributes.admin_roles is not None

    def test_all_three_admin_lists_may_be_cleared_at_once(self) -> None:
        """Atlas permits it — it orphans the connection, but it is legal."""
        ref = ConnectionRef.model_validate(
            {
                "typeName": "Connection",
                "attributes": {
                    "qualifiedName": "default/sf/123",
                    "name": "c",
                    "adminUsers": None,
                    "adminRoles": None,
                    "adminGroups": None,
                },
            }
        )

        assert ref.attributes.admin_users is None
        assert ref.attributes.admin_roles is None
        assert ref.attributes.admin_groups is None

    def test_a_cleared_admin_list_round_trips_back_to_the_struct(self) -> None:
        """``None`` must reach the wire as ``null``, not as ``[]``."""
        conn = _make_conn(admin_users=["alice"])
        conn.admin_roles = None  # type: ignore[attr-defined]

        back = ConnectionRef.from_connection(conn).to_connection()

        assert back.admin_roles is None

    @pytest.mark.parametrize("field", ["qualifiedName", "name"])
    def test_a_null_identity_field_is_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError):
            ConnectionRef.model_validate(
                {
                    "typeName": "Connection",
                    "attributes": {"qualifiedName": "default/sf/123", field: None},
                }
            )

    def test_optional_scalars_keep_an_explicit_null(self) -> None:
        """``connector_name`` / ``category`` are ``str | None`` already."""
        ref = ConnectionRef.model_validate(
            {
                "typeName": "Connection",
                "attributes": {"connectorName": None, "category": None},
            }
        )

        assert ref.attributes.connector_name is None
        assert ref.attributes.category is None


# ---------------------------------------------------------------------------
# admin_* states, end to end
# ---------------------------------------------------------------------------


ABSENT = "<absent>"
"""Sentinel for "this key is not in the mapping at all"."""


def _bare_conn() -> Any:
    """A ``Connection`` struct with *no* ``admin_*`` field touched at all.

    Distinct from ``_make_conn()``, which writes ``[]`` into all three. That
    difference is the point of :class:`TestAdminListStatesEndToEnd`.
    """
    return get_type("Connection")(name="c", qualified_name="default/sf/123")


class TestAdminListStatesEndToEnd:
    """What each ``admin_users`` state becomes at every hop.

    Four states on the ``pyatlan_v9`` struct, each distinct at every hop:

    ===============  ===================  =============  ===============
    struct           ``to_atlas_format``  model          ``model_dump``
    ===============  ===================  =============  ===============
    ``UNSET``        absent               ``[]``         absent
    ``= None``       ``null``             ``None``       ``null``
    ``= []``         ``[]``               ``[]``         ``[]``
    ``= ["alice"]``  ``["alice"]``        ``["alice"]``  ``["alice"]``
    ===============  ===================  =============  ===============

    Pinned as a table rather than as scattered cases because the property
    worth defending is that no two rows **collide**, and a test per state
    cannot see a collision. Both near-misses are real regressions this file
    has already caught once:

    * ``None`` is the only clear. Writing ``conn.admin_users = None`` is what
      expresses "remove every admin user"; leaving the field alone does not.
      It survives every hop, which is what pyatlan 11.3.0 bought — see
      ``TestPyatlanFlattenContract`` in
      ``tests/unit/common/test_entity_envelope.py``.
    * ``UNSET`` is absent, not ``[]``. The model column is deliberately *not*
      the dump column: the default is there so Python readers can iterate
      without a ``None`` check, and it is not a claim about the Connection.
      ``ConnectionAttributes._omit_unset_fields`` is what keeps the two apart.
    """

    @pytest.mark.parametrize(
        ("mutate", "expected_atlas", "expected_py", "expected_dump"),
        [
            pytest.param(lambda c: None, ABSENT, [], ABSENT, id="unset"),
            pytest.param(
                lambda c: setattr(c, "admin_users", None),
                None,
                None,
                None,
                id="cleared",
            ),
            pytest.param(
                lambda c: setattr(c, "admin_users", []), [], [], [], id="empty"
            ),
            pytest.param(
                lambda c: setattr(c, "admin_users", ["alice"]),
                ["alice"],
                ["alice"],
                ["alice"],
                id="populated",
            ),
        ],
    )
    def test_state_survives_every_hop(
        self,
        mutate: Any,
        expected_atlas: Any,
        expected_py: Any,
        expected_dump: Any,
    ) -> None:
        conn = _bare_conn()
        mutate(conn)

        atlas = to_atlas_format(conn)["attributes"].get("adminUsers", ABSENT)
        ref = ConnectionRef.from_connection(conn)
        dumped = ref.model_dump(by_alias=True)["attributes"].get("adminUsers", ABSENT)

        assert atlas == expected_atlas
        assert ref.attributes.admin_users == expected_py
        assert dumped == expected_dump

    def test_a_connection_with_no_admin_fields_mentions_none_of_them(self) -> None:
        """A key the Connection never had must not appear in the dump.

        The defaults are for Python readers, not claims about the Connection.
        Before ``_omit_unset_fields`` this emitted all three as ``[]`` — plus
        ``connectorName: null`` and ``category: null``, two fabricated clears.
        """
        ref = ConnectionRef.from_connection(_bare_conn())

        dumped = ref.model_dump(by_alias=True)["attributes"]

        assert "adminUsers" not in dumped
        assert "adminRoles" not in dumped
        assert "adminGroups" not in dumped
        assert "connectorName" not in dumped
        assert "category" not in dumped

    def test_the_python_default_is_still_a_list(self) -> None:
        """Omitted from the *dump*, not taken away from the reader.

        ``attrs.admin_users`` still iterates without a ``None`` check; that is
        what the default is for.
        """
        attrs = ConnectionRef.from_connection(_bare_conn()).attributes

        assert attrs.admin_users == []
        assert attrs.connector_name is None

    def test_an_unmentioned_field_stays_unset_through_a_round_trip(self) -> None:
        """absent → absent, all the way back to the struct."""
        back = ConnectionRef.from_connection(_bare_conn()).to_connection()

        assert repr(back.admin_users) == "UNSET"
        assert repr(back.connector_name) == "UNSET"

    def test_an_explicitly_empty_list_is_still_emitted(self) -> None:
        """``[]`` is a claim the caller made; only *absence* is dropped."""
        conn = _bare_conn()
        conn.admin_users = []

        dumped = ConnectionRef.from_connection(conn).model_dump(by_alias=True)

        assert dumped["attributes"]["adminUsers"] == []

    def test_extras_are_never_dropped(self) -> None:
        """An extra exists only because it was in the input, so it is set."""
        ref = ConnectionRef.model_validate(
            {
                "typeName": "Connection",
                "attributes": {"qualifiedName": "q", "sourceLogo": "logo.png"},
            }
        )

        dumped = ref.model_dump(by_alias=True)["attributes"]

        assert dumped["sourceLogo"] == "logo.png"
        assert "adminUsers" not in dumped

    def test_a_cleared_list_serialises_to_json_null(self) -> None:
        """Asserted as bytes, because "serialises as null" is a wire claim.

        A dict-level ``is None`` would also pass if the key were being
        dropped, which is the failure this exists to catch.
        """
        conn = _bare_conn()
        conn.admin_users = None

        ref = ConnectionRef.from_connection(conn)
        wire = orjson.loads(orjson.dumps(ref.model_dump(by_alias=True)))

        assert "adminUsers" in wire["attributes"]
        assert wire["attributes"]["adminUsers"] is None

    def test_cleared_and_empty_do_not_collapse(self) -> None:
        """``None`` means "remove them", ``[]`` means "there are none"."""
        cleared, empty = _bare_conn(), _bare_conn()
        cleared.admin_users = None
        empty.admin_users = []

        cleared_ref = ConnectionRef.from_connection(cleared)
        empty_ref = ConnectionRef.from_connection(empty)

        assert cleared_ref.attributes.admin_users is None
        assert empty_ref.attributes.admin_users == []
        assert cleared_ref.attributes != empty_ref.attributes

    def test_each_admin_field_can_be_cleared_independently(self) -> None:
        """The ACL edit that motivates all of this.

        Clear ``admin_roles``, grant ``admin_groups``, leave ``admin_users``
        alone — one wire payload has to carry all three states at once.
        """
        conn = _bare_conn()
        conn.admin_users = ["alice"]
        conn.admin_roles = None
        conn.admin_groups = ["data-platform"]

        attrs = ConnectionRef.from_connection(conn).model_dump(by_alias=True)[
            "attributes"
        ]

        assert attrs["adminUsers"] == ["alice"]
        assert attrs["adminRoles"] is None
        assert attrs["adminGroups"] == ["data-platform"]


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
