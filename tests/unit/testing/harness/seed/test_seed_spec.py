"""The spec's contract: exact qualified names, exact types, and no silent drift.

Every assertion here is a *literal* — the QNs and type names are spelled out
rather than recomposed from the spec, because recomposing them with the same
rule the code uses would pass whatever that rule became. Byte-exactness is the
whole reason this module exists: Atlas resolves a lineage ref by exact-match
qualified name and exact type, so a segment rule that drifts is a seed that
resolves nothing while every test still passes.

Tenant-free by construction: nothing here reaches for a client.
"""

from __future__ import annotations

import pytest

from application_sdk.testing.harness.atlas._errors import UnknownConnectorTypeError
from application_sdk.testing.harness.seed import (
    DatabaseSpec,
    SchemaSpec,
    SeedSegmentInvalidError,
    SeedSpec,
    TableSpec,
    connection_entity,
    ndjson_bytes,
    skeleton_assets,
)

_CONNECTION_QN = "default/snowflake/1787587123106596"


def _spec(**overrides: object) -> SeedSpec:
    """A two-level tree with one Table (two columns) and one View."""
    base: dict[str, object] = {
        "connector_type": "snowflake",
        "qualified_name": _CONNECTION_QN,
        "display_name": "snowflake-seed",
        "databases": (
            DatabaseSpec(
                name="ANALYTICS",
                schemas=(
                    SchemaSpec(
                        name="PUBLIC",
                        tables=(
                            TableSpec(name="ORDERS", columns=("ID", "AMOUNT")),
                            TableSpec(name="ORDERS_V", type_name="View"),
                        ),
                    ),
                ),
            ),
        ),
    }
    base.update(overrides)
    return SeedSpec(**base)  # type: ignore[arg-type]


def _resolved(**overrides: object):
    spec = _spec(**overrides)
    return spec.resolve(
        qualified_name=spec.qualified_name or "default/snowflake/minted",
        display_name=spec.display_name or "snowflake-minted",
    )


class TestQualifiedNameExactness:
    """``{connection}/{db}/{schema}/{table}[/{column}]``, segment for segment."""

    def test_every_qualified_name_is_the_one_the_spec_declares(self) -> None:
        assets = skeleton_assets(_resolved())
        assert [a.qualified_name for a in assets] == [
            "default/snowflake/1787587123106596/ANALYTICS",
            "default/snowflake/1787587123106596/ANALYTICS/PUBLIC",
            "default/snowflake/1787587123106596/ANALYTICS/PUBLIC/ORDERS",
            "default/snowflake/1787587123106596/ANALYTICS/PUBLIC/ORDERS/ID",
            "default/snowflake/1787587123106596/ANALYTICS/PUBLIC/ORDERS/AMOUNT",
            "default/snowflake/1787587123106596/ANALYTICS/PUBLIC/ORDERS_V",
        ]

    def test_case_is_carried_through_untouched(self) -> None:
        """Snowflake refs are ``.upper()``-d; a folded segment resolves nothing."""
        assets = skeleton_assets(
            _resolved(databases=(DatabaseSpec(name="MixedCase_DB"),))
        )
        assert assets[0].qualified_name == (
            "default/snowflake/1787587123106596/MixedCase_DB"
        )
        assert assets[0].name == "MixedCase_DB"


class TestTypeStrictness:
    """A ``Table`` never resolves a ref that said ``View``."""

    def test_the_declared_type_is_the_emitted_type(self) -> None:
        assets = skeleton_assets(_resolved())
        assert [a.type_name for a in assets] == [
            "Database",
            "Schema",
            "Table",
            "Column",
            "Column",
            "View",
        ]

    def test_a_views_columns_hang_off_the_view(self) -> None:
        """The column's parent reference names the parent's *type*, so a view
        seeded with table-typed columns leaves the columns unresolvable."""
        assets = skeleton_assets(
            _resolved(
                databases=(
                    DatabaseSpec(
                        name="D",
                        schemas=(
                            SchemaSpec(
                                name="S",
                                tables=(
                                    TableSpec(
                                        name="V", type_name="View", columns=("C",)
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            )
        )
        column = assets[-1]
        assert column.type_name == "Column"
        assert column.view is not None
        assert column.view.qualified_name == (
            "default/snowflake/1787587123106596/D/S/V"
        )


class TestEmissionOrder:
    """Parents strictly before children."""

    def test_the_tree_is_emitted_top_down(self) -> None:
        assets = skeleton_assets(_resolved())
        seen: set[str] = set()
        for asset in assets:
            qn = asset.qualified_name
            parent = qn.rsplit("/", 1)[0]
            # The connection itself is not in the batch (publish creates it), so
            # a database's parent is legitimately absent.
            if parent != _CONNECTION_QN:
                assert parent in seen, f"{qn} preceded its parent {parent}"
            seen.add(qn)


class TestNdjsonShape:
    """What publish and the validator actually decode."""

    def test_records_are_flattened_into_attributes_with_no_guid(self) -> None:
        import orjson

        lines = ndjson_bytes(skeleton_assets(_resolved())).splitlines()
        record = orjson.loads(lines[0])
        assert record["typeName"] == "Database"
        assert "guid" not in record
        assert record["attributes"]["qualifiedName"] == (
            "default/snowflake/1787587123106596/ANALYTICS"
        )

    def test_relationship_references_live_in_attributes(self) -> None:
        """Where connector ``serialize_entity`` puts them, and where
        ``from_atlas_json`` looks — the strict decoder reads only
        ``relationshipAttributes`` and would drop them."""
        import orjson

        lines = ndjson_bytes(skeleton_assets(_resolved())).splitlines()
        schema = orjson.loads(lines[1])
        assert schema["attributes"]["database"]["uniqueAttributes"] == {
            "qualifiedName": "default/snowflake/1787587123106596/ANALYTICS"
        }

    def test_the_payload_is_newline_delimited_with_a_trailing_newline(self) -> None:
        payload = ndjson_bytes(skeleton_assets(_resolved()))
        assert payload.endswith(b"\n")
        assert len(payload.splitlines()) == 6


class TestConnectionEntity:
    """What publish creates the seeded connection from."""

    def test_identity_and_acl_are_carried_verbatim(self) -> None:
        entity = connection_entity(
            _resolved(admin_users=("u",), admin_groups=("g",), admin_roles=("r",))
        )
        assert entity == {
            "typeName": "Connection",
            "attributes": {
                "name": "snowflake-seed",
                "qualifiedName": _CONNECTION_QN,
                "connectorName": "snowflake",
                "category": "warehouse",
                "adminUsers": ["u"],
                "adminGroups": ["g"],
                "adminRoles": ["r"],
            },
        }


class TestSpecValidation:
    """A segment that cannot compose is caught before anything is written."""

    def test_an_unknown_connector_type_is_named(self) -> None:
        with pytest.raises(UnknownConnectorTypeError) as caught:
            _spec(connector_type="not-a-connector").resolve(
                qualified_name=_CONNECTION_QN, display_name="x"
            )
        assert "not-a-connector" in str(caught.value)

    @pytest.mark.parametrize("segment", ["", "A/B", " A", "A "])
    def test_a_segment_that_cannot_compose_is_rejected(self, segment: str) -> None:
        with pytest.raises(SeedSegmentInvalidError):
            _spec(databases=(DatabaseSpec(name=segment),)).resolve(
                qualified_name=_CONNECTION_QN, display_name="x"
            )

    def test_a_bad_column_segment_is_rejected_too(self) -> None:
        """The deepest level is the one a spec author hand-lists, so it is the
        one most likely to carry a stray separator."""
        with pytest.raises(SeedSegmentInvalidError):
            _spec(
                databases=(
                    DatabaseSpec(
                        name="D",
                        schemas=(
                            SchemaSpec(
                                name="S",
                                tables=(TableSpec(name="T", columns=("A/B",)),),
                            ),
                        ),
                    ),
                )
            ).resolve(qualified_name=_CONNECTION_QN, display_name="x")

    @pytest.mark.parametrize(
        "qualified_name", ["snowflake/123", "default//123", "default/snowflake/"]
    )
    def test_a_malformed_connection_qn_is_rejected(self, qualified_name: str) -> None:
        """``atlan-publish-app`` refuses connection creation on anything that is
        not a slash-delimited path, minutes into the run."""
        with pytest.raises(SeedSegmentInvalidError):
            _spec().resolve(qualified_name=qualified_name, display_name="x")

    def test_a_resolved_spec_keeps_the_identity_it_was_given(self) -> None:
        resolved = _spec(qualified_name=None, display_name=None).resolve(
            qualified_name="default/snowflake/minted",
            display_name="snowflake-minted",
        )
        assert resolved.qualified_name == "default/snowflake/minted"
        assert resolved.display_name == "snowflake-minted"
