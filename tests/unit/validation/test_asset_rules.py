"""Tests for the opt-in create-time rules in application_sdk.validation.asset_rules.

pyatlan 11.5.0 dropped the SQL-parent ``for_creation`` checks. Each test here is
red-green against that: the bare-parent asset passes ``validate_asset`` by default
(pyatlan's behaviour, pinned so a pyatlan change that restores the checks is
noticed) and fails it with the rule set named.
"""

from __future__ import annotations

import json
from pathlib import Path

import msgspec
import pytest
from pyatlan_v9.model.assets import (
    Column,
    MongoDBCollection,
    Procedure,
    Schema,
    Table,
    View,
)
from pyatlan_v9.model.assets.sql_related import RelatedSchema, RelatedTable

from application_sdk.validation import AssetCreationRules
from application_sdk.validation import asset_rules as rules_module
from application_sdk.validation import asset_validation_summary_json
from application_sdk.validation import assets as assets_module
from application_sdk.validation import (
    validate_asset,
    validate_assets_as_artifact,
    validate_transformed_dir,
)

SQL = frozenset({AssetCreationRules.SQL_PARENTS})
CONN = "default/mssql/1700000000"
DB_QN = f"{CONN}/DB"
SCHEMA_QN = f"{DB_QN}/SCHEMA"
TABLE_QN = f"{SCHEMA_QN}/T1"

SCHEMA_AND_DATABASE = [
    "schema_name is required for creation",
    "schema_qualified_name is required for creation",
    "database_name is required for creation",
    "database_qualified_name is required for creation",
]


def _bare(cls: type, qualified_name: str):
    """An asset carrying only what a transform that forgot its parents emits."""
    return cls(
        name=qualified_name.rsplit("/", 1)[-1],
        qualified_name=qualified_name,
        connection_qualified_name=CONN,
    )


def _as_connector_writes_it(asset) -> bytes:
    """Serialize the way connector apps do — relationships moved into
    ``attributes``. Mirrors the helper in ``test_assets.py``."""
    data = json.loads(asset.to_nested_bytes())
    rels = data.pop("relationshipAttributes", None) or {}
    for key, value in rels.items():
        if value is not None:
            data.setdefault("attributes", {})[key] = value
    return json.dumps(data).encode()


def _write(base: Path, entity: str, assets: list) -> None:
    out_dir = base / "transformed" / entity
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "entities.json", "wb") as handle:
        for asset in assets:
            handle.write(_as_connector_writes_it(asset))
            handle.write(b"\n")


# ---------------------------------------------------------------------------
# validate_asset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "qualified_name", "expected"),
    [
        (
            Schema,
            SCHEMA_QN,
            [
                "database is required for creation",
                "database_name is required for creation",
                "database_qualified_name is required for creation",
            ],
        ),
        (
            Table,
            TABLE_QN,
            ["atlan_schema is required for creation", *SCHEMA_AND_DATABASE],
        ),
        (View, f"{SCHEMA_QN}/V1", SCHEMA_AND_DATABASE),
        (
            MongoDBCollection,
            f"{DB_QN}/C1",
            [
                "database_name is required for creation",
                "database_qualified_name is required for creation",
            ],
        ),
    ],
    ids=["Schema", "Table", "View", "MongoDBCollection"],
)
def test_missing_parents_pass_by_default_and_fail_when_opted_in(
    cls: type, qualified_name: str, expected: list[str]
) -> None:
    asset = _bare(cls, qualified_name)

    default = validate_asset(asset)
    assert not any(message in e for e in default for message in expected), default

    assert validate_asset(asset, creation_rules=SQL) == default + expected


def test_column_rules_ride_alongside_pyatlans_own_errors() -> None:
    # A Column also misses checks pyatlan still runs (its table, order); those
    # arrive as pyatlan's bundle, the rule messages after it.
    asset = _bare(Column, f"{TABLE_QN}/C1")
    default = validate_asset(asset)
    assert len(default) == 1 and "order is required for creation" in default[0]
    assert validate_asset(asset, creation_rules=SQL) == default + SCHEMA_AND_DATABASE


def test_assets_built_by_creators_pass_the_rules() -> None:
    # The creators populate every parent field, so opting in adds no false
    # positives on well-formed output.
    built = [
        Schema.creator(name="SCHEMA", database_qualified_name=DB_QN),
        Table.creator(name="T1", schema_qualified_name=SCHEMA_QN),
        View.creator(name="V1", schema_qualified_name=SCHEMA_QN),
        Column.creator(
            name="C1", parent_type=Table, parent_qualified_name=TABLE_QN, order=1
        ),
        Procedure.creator(name="P1", schema_qualified_name=SCHEMA_QN, definition="x"),
    ]
    for asset in built:
        assert validate_asset(asset, creation_rules=SQL) == [], type(asset).__name__


def test_rules_apply_only_for_creation() -> None:
    asset = _bare(Table, TABLE_QN)
    assert validate_asset(asset, for_creation=False, creation_rules=SQL) == []


def test_explicit_null_is_not_flagged() -> None:
    # pyatlan 11.4.1 flagged only UNSET (absent) fields; the rules keep that.
    asset = _bare(Schema, SCHEMA_QN)
    asset.database = None
    asset.database_name = None
    asset.database_qualified_name = None
    assert validate_asset(asset, creation_rules=SQL) == []


def test_set_parents_satisfy_the_rules() -> None:
    asset = _bare(Table, TABLE_QN)
    asset.atlan_schema = RelatedSchema(qualified_name=SCHEMA_QN)
    asset.schema_name = "SCHEMA"
    asset.schema_qualified_name = SCHEMA_QN
    asset.database_name = "DB"
    asset.database_qualified_name = DB_QN
    assert validate_asset(asset, creation_rules=SQL) == []


def test_unlisted_types_are_untouched() -> None:
    # A type the rule set does not name gets no extra messages.
    from pyatlan_v9.model.assets import Database

    asset = _bare(Database, DB_QN)
    assert validate_asset(asset, creation_rules=SQL) == validate_asset(asset)


# ---------------------------------------------------------------------------
# The rule table itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "cls"),
    [(rule, cls) for rule, table in rules_module._RULE_FIELDS.items() for cls in table],
    ids=lambda v: v.__name__ if isinstance(v, type) else str(v),
)
def test_every_rule_field_exists_on_its_type(
    rule: AssetCreationRules, cls: type
) -> None:
    """A pyatlan rename would otherwise turn a rule into one that always fires
    (``getattr`` default is UNSET) — caught here instead of in a connector run."""
    names = {f.name for f in msgspec.structs.fields(cls)}
    missing = set(rules_module._RULE_FIELDS[rule][cls]) - names
    assert not missing, f"{cls.__name__} has no field(s) {sorted(missing)}"


def test_every_rule_set_has_a_table() -> None:
    assert set(rules_module._RULE_FIELDS) == set(AssetCreationRules)


def test_sql_parents_covers_the_types_pyatlan_dropped() -> None:
    """Guard against the table being trimmed and the opt-in going quietly weak.
    The 24 types are those whose ``for_creation`` SQL-parent checks pyatlan
    11.5.0 removed (diffed against 11.4.1)."""
    table = rules_module._RULE_FIELDS[AssetCreationRules.SQL_PARENTS]
    assert len(table) == 24
    assert {Schema, Table, View, Column, Procedure} <= set(table)


# ---------------------------------------------------------------------------
# Batch paths and the per-field summary
# ---------------------------------------------------------------------------


def test_validate_transformed_dir_threads_the_rules(tmp_path: Path) -> None:
    _write(tmp_path, "Schema", [_bare(Schema, SCHEMA_QN)])

    assert validate_transformed_dir(tmp_path).ok

    report = validate_transformed_dir(tmp_path, creation_rules=SQL)
    assert not report.ok
    assert report.failures[0].errors == [
        "database is required for creation",
        "database_name is required for creation",
        "database_qualified_name is required for creation",
    ]


def test_summary_keys_rule_messages_per_field(tmp_path: Path) -> None:
    _write(tmp_path, "Table", [_bare(Table, TABLE_QN)])
    report = validate_transformed_dir(
        tmp_path, creation_rules=SQL, check_referential_integrity=False
    )
    details = {
        row["detail"]
        for row in json.loads(asset_validation_summary_json(report))
        if row["type_name"] == "Table"
    }
    assert {
        "required_for_creation:atlan_schema",
        "required_for_creation:schema_name",
        "required_for_creation:schema_qualified_name",
        "required_for_creation:database_name",
        "required_for_creation:database_qualified_name",
    } <= details


def test_validate_assets_as_artifact_threads_the_rules(tmp_path: Path) -> None:
    _write(tmp_path, "Schema", [_bare(Schema, SCHEMA_QN)])
    assert validate_assets_as_artifact(tmp_path).assets.ok
    assert not validate_assets_as_artifact(tmp_path, creation_rules=SQL).assets.ok


# ---------------------------------------------------------------------------
# Production signatures pyatlan 11.5.0 stopped reporting
# ---------------------------------------------------------------------------

OPT_IN_RELATIONSHIP_SIGNATURES = [
    # (connector, class, relationship, Related* class, the EXACT error production logged)
    ("mssql", Schema, "database", None, "database is required for creation"),
    (
        "athena",
        Table,
        "atlan_schema",
        RelatedSchema,
        "atlan_schema is required for creation",
    ),
]


@pytest.mark.parametrize(
    ("connector", "cls", "relationship", "related_cls", "prod_error"),
    OPT_IN_RELATIONSHIP_SIGNATURES,
    ids=[f"{c}-{t.__name__}.{r}" for c, t, r, _, _ in OPT_IN_RELATIONSHIP_SIGNATURES],
)
def test_prod_signature_is_reported_again_when_opted_in(
    connector: str,
    cls: type,
    relationship: str,
    related_cls: type | None,
    prod_error: str,
) -> None:
    """The mssql ``Schema.database`` and athena ``Table.atlan_schema`` production
    failures, through the connector-shaped round trip: silent by default on
    pyatlan 11.5.0, reported with the rule set, and cleared once the payload
    carries the relationship."""
    from pyatlan_v9.model.assets.sql_related import RelatedDatabase

    related_cls = related_cls or RelatedDatabase
    parent_qn = f"default/{connector}/1700000000/parent"
    missing = assets_module._deserialize(
        _as_connector_writes_it(_bare(cls, f"{parent_qn}/thing"))
    )
    assert getattr(missing, relationship) is msgspec.UNSET
    assert not any(prod_error in e for e in validate_asset(missing))
    assert prod_error in validate_asset(missing, creation_rules=SQL)

    present = _bare(cls, f"{parent_qn}/thing")
    setattr(present, relationship, related_cls(qualified_name=parent_qn))
    decoded = assets_module._deserialize(_as_connector_writes_it(present))
    assert getattr(decoded, relationship).qualified_name == parent_qn
    assert prod_error not in validate_asset(decoded, creation_rules=SQL)


def test_related_table_import_is_the_one_rules_compare_against() -> None:
    # Column's rule set does not include its table — pyatlan still reports that
    # ("one of table, ..."). Pin it so the rule table is not widened to duplicate it.
    asset = _bare(Column, f"{TABLE_QN}/C1")
    asset.table = RelatedTable(qualified_name=TABLE_QN)
    errors = validate_asset(asset, creation_rules=SQL)
    assert not any("one of table" in e for e in errors)
    assert errors[-4:] == SCHEMA_AND_DATABASE
