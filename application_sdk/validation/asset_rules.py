"""Opt-in create-time checks that pyatlan_v9's own ``.validate()`` no longer runs.

pyatlan 11.5.0 derives SQL parents from the qualifiedName in its ``creator()``
helpers, and so dropped the matching ``for_creation`` checks: a Schema without its
database, a Table without its schema, a Column without its schema/database
denormalised names all pass ``.validate(for_creation=True)`` now. Assets built
any other way — a connector transform writing NDJSON — still need those fields
to publish, and earlier production failures were exactly this missing-parent
shape. The default stays aligned with pyatlan; an app opts back in by naming a
rule set (:class:`AssetCreationRules`) on
:func:`~application_sdk.validation.assets.validate_asset`,
:func:`~application_sdk.validation.assets.validate_transformed_dir`, the
integration test's ``asset_validation_rules`` or the app's
``asset_validation_rules``.

Each rule restores a check pyatlan 11.4.1 ran, with its exact semantics — the
field is flagged only when ``UNSET`` (absent from the record), not when it is an
explicit ``null`` — and its exact message, ``"<field> is required for creation"``,
so the per-field summary keys (``required_for_creation:<field>``) are unchanged.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Mapping

import msgspec
from pyatlan_v9.model.assets import (
    Asset,
    BigqueryRoutine,
    CalculationView,
    Column,
    CosmosMongoDBCollection,
    DatabricksAIModelContext,
    DatabricksAIModelVersion,
    DatabricksVolume,
    DatabricksVolumePath,
    DocumentDBCollection,
    Function,
    MaterialisedView,
    MongoDBCollection,
    Procedure,
    Schema,
    SnowflakeAIModelContext,
    SnowflakeAIModelVersion,
    SnowflakeSemanticDimension,
    SnowflakeSemanticFact,
    SnowflakeSemanticLogicalTable,
    SnowflakeSemanticMetric,
    SnowflakeSemanticView,
    Table,
    TablePartition,
    View,
)


class AssetCreationRules(StrEnum):
    """Named sets of extra create-time checks, each off unless an app asks for it.

    Values are stable identifiers; add a member rather than widening one.
    """

    SQL_PARENTS = "sql_parents"
    """Schema/database parents of SQL-family assets: the ``database*`` and
    ``schema*`` checks pyatlan 11.5.0 dropped, plus ``Schema.database`` and
    ``Table.atlan_schema``."""


_DATABASE: Final = ("database_name", "database_qualified_name")
_SCHEMA_AND_DATABASE: Final = ("schema_name", "schema_qualified_name", *_DATABASE)

# Field order follows pyatlan 11.4.1's ``validate()`` for each type, so a record
# reports its messages in the order it used to.
_SQL_PARENT_FIELDS: Final[Mapping[type[Asset], tuple[str, ...]]] = {
    Schema: ("database", *_DATABASE),
    Table: ("atlan_schema", *_SCHEMA_AND_DATABASE),
    View: _SCHEMA_AND_DATABASE,
    MaterialisedView: _SCHEMA_AND_DATABASE,
    TablePartition: _SCHEMA_AND_DATABASE,
    Column: _SCHEMA_AND_DATABASE,
    Procedure: _SCHEMA_AND_DATABASE,
    Function: _SCHEMA_AND_DATABASE,
    CalculationView: _SCHEMA_AND_DATABASE,
    BigqueryRoutine: _SCHEMA_AND_DATABASE,
    DatabricksAIModelContext: _SCHEMA_AND_DATABASE,
    DatabricksAIModelVersion: _SCHEMA_AND_DATABASE,
    DatabricksVolume: _SCHEMA_AND_DATABASE,
    DatabricksVolumePath: _SCHEMA_AND_DATABASE,
    SnowflakeAIModelContext: _SCHEMA_AND_DATABASE,
    SnowflakeAIModelVersion: _SCHEMA_AND_DATABASE,
    SnowflakeSemanticDimension: _SCHEMA_AND_DATABASE,
    SnowflakeSemanticFact: _SCHEMA_AND_DATABASE,
    SnowflakeSemanticLogicalTable: _SCHEMA_AND_DATABASE,
    SnowflakeSemanticMetric: _SCHEMA_AND_DATABASE,
    SnowflakeSemanticView: _SCHEMA_AND_DATABASE,
    MongoDBCollection: _DATABASE,
    CosmosMongoDBCollection: _DATABASE,
    DocumentDBCollection: _DATABASE,
}

_RULE_FIELDS: Final[
    Mapping[AssetCreationRules, Mapping[type[Asset], tuple[str, ...]]]
] = {
    AssetCreationRules.SQL_PARENTS: _SQL_PARENT_FIELDS,
}


def creation_rule_errors(
    asset: Asset, rules: frozenset[AssetCreationRules]
) -> list[str]:
    """Messages for every field ``rules`` require on ``asset`` that it lacks.

    Matches on the exact class: pyatlan_v9's asset types do not subclass one
    another, so a rule names precisely the type it was dropped from.

    Args:
        asset: A concrete pyatlan_v9 asset instance.
        rules: The rule sets to apply; empty applies none.

    Returns:
        ``"<field> is required for creation"`` per missing field, in rule order.
    """
    errors: list[str] = []
    for rule in sorted(rules):
        for field_name in _RULE_FIELDS[rule].get(type(asset), ()):
            if getattr(asset, field_name, msgspec.UNSET) is msgspec.UNSET:
                errors.append(f"{field_name} is required for creation")
    return errors
