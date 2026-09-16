"""Tests for application_sdk.common.entity_envelope (FND-2137).

Three things are pinned here, in descending order of how much it costs to get
them wrong:

1. **The pyatlan contract.** The flattened envelope is ``to_atlas_format``'s
   output, not ours. ``pyatlan_v9.model.transform`` declares no ``__all__``, so
   a pyatlan bump that changes its ``top_level_keys`` partition changes the
   fleet's wire format with nothing in between. ``TestPyatlanFlattenContract``
   is that "nothing in between".
2. **The two flatten paths agree.** ``to_atlas_format`` for v9 assets, the
   generic pass for dicts and v1 models. "Flattened" meaning two different
   things depending on what the mapper returned is the same class of bug the
   module closes, so the paths are compared against each other, not each
   against its own expectation.
3. **The lever is inert.** ``PYATLAN`` must reproduce pre-FND-2137 output
   exactly, or it does not defer the re-publish it exists to defer.
"""

from __future__ import annotations

from typing import Any

import orjson
import pytest
from pyatlan_v9.model.assets import Column, Table, View
from pyatlan_v9.model.transform import to_atlas_format

from application_sdk.common.asset_serialization import entity_bytes
from application_sdk.common.entity_envelope import (
    DEFAULT_ENVELOPE,
    EntityDecorations,
    EntityEnvelopePolicy,
    EnvelopeShape,
    apply_envelope,
    flatten_envelope,
    to_atlas_format_dict,
)

SCHEMA_QN = "default/mysql/1234567890/db/sch"
TABLE_QN = f"{SCHEMA_QN}/T1"

PYATLAN_ENVELOPE = EntityEnvelopePolicy(shape=EnvelopeShape.PYATLAN)


def _table(name: str = "T1") -> Table:
    return Table.creator(name=name, schema_qualified_name=SCHEMA_QN)


def _column(name: str = "c1") -> Column:
    return Column.creator(
        name=name, parent_qualified_name=TABLE_QN, parent_type=Table, order=1
    )


def _view(name: str = "V1") -> View:
    return View.creator(name=name, schema_qualified_name=SCHEMA_QN)


class TestPyatlanFlattenContract:
    """What ``to_atlas_format`` guarantees, pinned so a bump can't move it.

    Each assertion here is a property the SDK's flattened envelope depends on.
    If one of these fails after a pyatlan upgrade, the fix is not to relax the
    test — it is to decide whether the fleet's wire format just changed.
    """

    def test_relationship_refs_land_inside_attributes(self):
        out = to_atlas_format(_column())

        assert out["attributes"]["table"]["uniqueAttributes"] == {
            "qualifiedName": TABLE_QN
        }

    def test_no_relationship_buckets_are_emitted(self):
        """The keys ``atlan-publish-app`` generates itself from its own diff.

        A producer-side value collides with the diff engine's, so the envelope
        must not carry them.
        """
        out = to_atlas_format(_column())

        assert "relationshipAttributes" not in out
        assert "appendRelationshipAttributes" not in out
        assert "removeRelationshipAttributes" not in out

    def test_nulls_dropped_from_attributes(self):
        asset = _table()
        asset.description = None

        assert "description" not in to_atlas_format(asset)["attributes"]

    def test_nulls_kept_inside_custom_attributes(self):
        """The asymmetry postgres relies on for v2 parity.

        A null in ``customAttributes`` is a value the source reported as empty
        and the v2 baseline carried. Dropping it is a diff against every
        previously published entity.
        """
        asset = _table()
        asset.custom_attributes = {"engine": "InnoDB", "row_format": None}

        assert to_atlas_format(asset)["customAttributes"]["row_format"] is None

    def test_status_and_custom_attributes_stay_at_the_root(self):
        asset = _table()
        asset.status = "ACTIVE"
        asset.custom_attributes = {}

        out = to_atlas_format(asset)

        assert out["status"] == "ACTIVE"
        assert out["customAttributes"] == {}

    def test_table_ddl_stays_under_table_definition(self):
        """``Table`` declares ``tableDefinition``; ``View`` declares
        ``definition``. Different attributes on different types — see
        FND-2148, where treating them as synonyms lost every table's DDL.
        """
        table = _table()
        table.table_definition = "CREATE TABLE ..."
        view = _view()
        view.definition = "CREATE VIEW ..."

        assert to_atlas_format(table)["attributes"]["tableDefinition"]
        assert "definition" not in to_atlas_format(table)["attributes"]
        assert to_atlas_format(view)["attributes"]["definition"]


class TestToAtlasFormatDict:
    def test_returns_none_for_a_plain_dict(self):
        """``None`` means "not a v9 asset", which routes to the generic pass."""
        assert to_atlas_format_dict({"typeName": "Table"}) is None

    def test_returns_none_for_an_unrelated_object(self):
        assert to_atlas_format_dict(object()) is None

    def test_delegates_for_a_v9_asset(self):
        asset = _column()

        assert to_atlas_format_dict(asset) == to_atlas_format(asset)


class TestFlattenEnvelope:
    def test_refs_move_into_attributes(self):
        entity = flatten_envelope(
            {
                "typeName": "Column",
                "attributes": {"name": "c1"},
                "relationshipAttributes": {"table": {"typeName": "Table"}},
            }
        )

        assert entity["attributes"]["table"] == {"typeName": "Table"}
        assert "relationshipAttributes" not in entity

    def test_append_and_remove_buckets_are_dropped_not_merged(self):
        entity = flatten_envelope(
            {
                "typeName": "Table",
                "attributes": {"name": "t"},
                "appendRelationshipAttributes": {"inputs": [{"typeName": "Table"}]},
                "removeRelationshipAttributes": {"outputs": []},
            }
        )

        assert entity == {"typeName": "Table", "attributes": {"name": "t"}}

    def test_null_refs_are_not_merged(self):
        entity = flatten_envelope(
            {
                "typeName": "Column",
                "attributes": {"name": "c1"},
                "relationshipAttributes": {"table": None},
            }
        )

        assert "table" not in entity["attributes"]

    def test_nulls_dropped_from_attributes_and_root(self):
        entity = flatten_envelope(
            {
                "typeName": "Table",
                "status": None,
                "attributes": {"name": "t", "description": None},
            }
        )

        assert entity == {"typeName": "Table", "attributes": {"name": "t"}}

    def test_nulls_kept_inside_custom_attributes(self):
        entity = flatten_envelope(
            {
                "typeName": "Table",
                "attributes": {"name": "t"},
                "customAttributes": {"engine": None},
            }
        )

        assert entity["customAttributes"] == {"engine": None}

    def test_a_ref_beats_a_hand_written_duplicate_in_attributes(self):
        """Documented collision rule, matching ``to_atlas_format``.

        The relationship field is the typed one; a string of the same name in
        ``attributes`` is the hand-written duplicate.
        """
        entity = flatten_envelope(
            {
                "typeName": "Column",
                "attributes": {"table": "hand-written"},
                "relationshipAttributes": {"table": {"typeName": "Table"}},
            }
        )

        assert entity["attributes"]["table"] == {"typeName": "Table"}

    def test_entity_with_no_relationships_is_unchanged(self):
        entity = flatten_envelope({"typeName": "Table", "attributes": {"name": "t"}})

        assert entity == {"typeName": "Table", "attributes": {"name": "t"}}


class TestBothFlattenPathsAgree:
    """The generic pass and ``to_atlas_format`` must produce the same envelope.

    A dict-returning mapper and an asset-returning mapper describing the same
    entity have to land on the same line, or ``FLATTENED`` means two things.
    """

    @pytest.mark.parametrize("factory", [_table, _column, _view])
    def test_generic_pass_over_nested_output_matches_to_atlas_format(self, factory):
        asset = factory()

        via_pyatlan = to_atlas_format(asset)
        via_generic = flatten_envelope(orjson.loads(asset.to_nested_bytes()))

        assert via_generic == via_pyatlan


class TestSqlDialect:
    def test_stamped_on_a_table_carrying_ddl(self):
        asset = _table()
        asset.table_definition = "CREATE TABLE ..."

        out = orjson.loads(
            entity_bytes(asset, envelope=EntityEnvelopePolicy(sql_dialect="teradata"))
        )

        assert out["attributes"]["sqlDialect"] == "teradata"

    def test_stamped_on_a_view_carrying_ddl(self):
        asset = _view()
        asset.definition = "CREATE VIEW ..."

        out = orjson.loads(
            entity_bytes(asset, envelope=EntityEnvelopePolicy(sql_dialect="teradata"))
        )

        assert out["attributes"]["sqlDialect"] == "teradata"

    def test_not_stamped_on_an_asset_with_no_ddl(self):
        """On an asset with no definition it is noise every downstream diff
        would have to carry."""
        out = orjson.loads(
            entity_bytes(
                _column(), envelope=EntityEnvelopePolicy(sql_dialect="teradata")
            )
        )

        assert "sqlDialect" not in out["attributes"]

    def test_no_pyatlan_field_holds_it(self):
        """Why the SDK has to stamp it at all.

        ``sqlDialect`` does not exist anywhere in ``pyatlan_v9`` — no asset
        type declares it — so an asset-returning mapper has nowhere to put it
        and cannot set it however it tries. That makes it the SDK's to inject,
        in the same sense as ``connectionName``.
        """
        asset = _view()

        with pytest.raises(AttributeError):
            asset.sql_dialect = "mapper-chose-this"  # type: ignore[attr-defined]

    def test_mapper_set_value_wins(self):
        """Only a dict-returning mapper can reach this — see the test above."""
        payload = {
            "typeName": "View",
            "attributes": {
                "name": "V1",
                "definition": "CREATE VIEW ...",
                "sqlDialect": "mapper-chose-this",
            },
        }

        out = orjson.loads(
            entity_bytes(payload, envelope=EntityEnvelopePolicy(sql_dialect="teradata"))
        )

        assert out["attributes"]["sqlDialect"] == "mapper-chose-this"

    def test_applies_under_the_pyatlan_lever_too(self):
        """The lever pins the envelope, not every other knob.

        A policy that asks for a dialect is not the identity policy, so it
        leaves the byte fast path even under ``PYATLAN``.
        """
        asset = _view()
        asset.definition = "CREATE VIEW ..."

        out = orjson.loads(
            entity_bytes(
                asset,
                envelope=EntityEnvelopePolicy(
                    shape=EnvelopeShape.PYATLAN, sql_dialect="teradata"
                ),
            )
        )

        assert out["attributes"]["sqlDialect"] == "teradata"
        assert "relationshipAttributes" in out


class TestDecorations:
    def test_land_at_the_entity_root_not_in_attributes(self):
        out = orjson.loads(
            entity_bytes(
                _view(),
                decorations=EntityDecorations(
                    default_catalog_name="db", default_schema_name="sch"
                ),
            )
        )

        assert out["defaultCatalogName"] == "db"
        assert out["defaultSchemaName"] == "sch"
        assert "defaultCatalogName" not in out["attributes"]

    def test_unset_fields_are_omitted(self):
        out = orjson.loads(
            entity_bytes(
                _view(), decorations=EntityDecorations(default_catalog_name="db")
            )
        )

        assert out["defaultCatalogName"] == "db"
        assert "defaultSchemaName" not in out

    def test_empty_decorations_add_nothing(self):
        # One asset, serialised twice: ``creator()`` mints a fresh negative
        # placeholder guid per call, so two instances never compare equal.
        asset = _view()

        decorated = orjson.loads(entity_bytes(asset, decorations=EntityDecorations()))
        plain = orjson.loads(entity_bytes(asset))

        assert decorated == plain

    def test_applied_under_the_pyatlan_lever_too(self):
        out = orjson.loads(
            entity_bytes(
                _view(),
                envelope=PYATLAN_ENVELOPE,
                decorations=EntityDecorations(default_catalog_name="db"),
            )
        )

        assert out["defaultCatalogName"] == "db"
        assert "relationshipAttributes" in out

    def test_as_root_fields_is_the_only_wire_spelling(self):
        assert EntityDecorations(
            default_catalog_name="db", default_schema_name="sch"
        ).as_root_fields() == {"defaultCatalogName": "db", "defaultSchemaName": "sch"}


class TestPolicyDefaults:
    def test_default_is_flattened(self):
        assert DEFAULT_ENVELOPE.shape is EnvelopeShape.FLATTENED
        assert DEFAULT_ENVELOPE.sql_dialect is None

    def test_default_policy_is_not_the_identity_policy(self):
        """The default must leave the byte fast path, or it would not flatten."""
        assert not DEFAULT_ENVELOPE.is_identity

    def test_bare_pyatlan_policy_is_the_identity_policy(self):
        assert PYATLAN_ENVELOPE.is_identity

    def test_a_dialect_defeats_the_identity_policy(self):
        assert not EntityEnvelopePolicy(
            shape=EnvelopeShape.PYATLAN, sql_dialect="teradata"
        ).is_identity

    def test_policy_is_frozen(self):
        with pytest.raises(Exception):
            DEFAULT_ENVELOPE.shape = EnvelopeShape.PYATLAN  # type: ignore[misc]


class TestEntityBytesEnvelopeIntegration:
    def test_default_flattens_a_v9_asset(self):
        out = orjson.loads(entity_bytes(_column()))

        assert out["attributes"]["table"]["uniqueAttributes"] == {
            "qualifiedName": TABLE_QN
        }
        assert "relationshipAttributes" not in out

    def test_lever_keeps_the_nested_shape(self):
        out = orjson.loads(entity_bytes(_column(), envelope=PYATLAN_ENVELOPE))

        assert out["relationshipAttributes"]["table"]["uniqueAttributes"] == {
            "qualifiedName": TABLE_QN
        }
        assert "table" not in out["attributes"]

    def test_default_flattens_a_plain_dict_mapper_result(self):
        payload: dict[str, Any] = {
            "typeName": "Column",
            "attributes": {"name": "c1"},
            "relationshipAttributes": {"table": {"typeName": "Table"}},
        }

        out = orjson.loads(entity_bytes(payload))

        assert out["attributes"]["table"] == {"typeName": "Table"}
        assert "relationshipAttributes" not in out

    def test_framework_attributes_survive_the_envelope_pass(self):
        """connectionName (FND-2056) is stamped before dispatch; flattening
        must not drop it on the way out."""
        out = orjson.loads(entity_bytes(_column(), connection_name="my-conn"))

        assert out["attributes"]["connectionName"] == "my-conn"


class TestApplyEnvelope:
    def test_already_flattened_skips_the_generic_pass(self):
        """``to_atlas_format`` output has no buckets left to merge, so the pass
        is a no-op — but it would still re-drop nulls over every record."""
        entity = {"typeName": "Table", "relationshipAttributes": {"x": 1}}

        out = apply_envelope(
            dict(entity), policy=DEFAULT_ENVELOPE, already_flattened=True
        )

        assert out == entity

    def test_pyatlan_shape_does_not_flatten(self):
        entity = {"typeName": "Table", "relationshipAttributes": {"x": 1}}

        out = apply_envelope(dict(entity), policy=PYATLAN_ENVELOPE)

        assert out == entity
