"""Tests for application_sdk.validation.assets.

Fixtures are built from real pyatlan_v9 asset creators and serialized with
``to_nested_bytes()`` — the same wire shape connectors write to
``transformed/<Entity>/entities.json`` — so the read-back path is exercised
exactly as it runs in production.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock, patch

import msgspec
import pytest
from pyatlan_v9.model.assets import Column, Database, Schema, Table, View

from application_sdk.validation import AssetValidationReport, ReferentialFailure
from application_sdk.validation import assets as assets_module
from application_sdk.validation import validate_asset, validate_transformed_dir

_HAS_ROCKSDICT = importlib.util.find_spec("rocksdict") is not None
requires_rocksdict = pytest.mark.skipif(
    not _HAS_ROCKSDICT,
    reason="referential-integrity pass needs rocksdict (the [storage] extra)",
)

CONN = "default/snow/123"
DB_QN = f"{CONN}/DB"
SCHEMA_QN = f"{CONN}/DB/SCHEMA"
TABLE_QN = f"{CONN}/DB/SCHEMA/T1"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _database() -> Database:
    return Database.creator(name="DB", connection_qualified_name=CONN)


def _schema() -> Schema:
    return Schema.creator(name="SCHEMA", database_qualified_name=DB_QN)


def _table(name: str = "T1") -> Table:
    return Table.creator(name=name, schema_qualified_name=SCHEMA_QN)


def _column(name: str, parent_table_qn: str) -> Column:
    return Column.creator(
        name=name,
        parent_type=Table,
        parent_qualified_name=parent_table_qn,
        order=1,
    )


def _write(
    base: Path, entity: str, assets: list, *, extra_lines: list[bytes] | None = None
) -> None:
    """Write assets (and optional raw extra lines) as NDJSON under transformed/<entity>/."""
    out_dir = base / "transformed" / entity
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "entities.json", "wb") as handle:
        for asset in assets:
            handle.write(asset.to_nested_bytes())
            handle.write(b"\n")
        for raw in extra_lines or []:
            handle.write(raw)
            handle.write(b"\n")


# ---------------------------------------------------------------------------
# validate_asset
# ---------------------------------------------------------------------------


class TestValidateAsset:
    def test_valid_asset_returns_no_errors(self) -> None:
        assert validate_asset(_table()) == []

    def test_missing_qualified_name_is_reported(self) -> None:
        table = _table()
        table.qualified_name = None
        errors = validate_asset(table)
        assert errors
        assert any("qualified_name" in message for message in errors)

    def test_never_raises(self) -> None:
        table = _table()
        table.qualified_name = None
        # Must return messages, not raise.
        assert validate_asset(table) != []

    def test_never_raises_on_non_value_error(self) -> None:
        # The widened ``except Exception`` must swallow more than the ValueError
        # a real ``.validate()`` raises: any error surfaces as a message.
        asset = Mock()
        asset.validate.side_effect = RuntimeError("boom")
        assert validate_asset(asset) == ["boom"]

    def test_for_creation_enforces_hierarchy_fields(self) -> None:
        # A bare Column (no parent refs) is valid at rest but not for creation.
        column = Column()
        column.name = "C1"
        column.qualified_name = f"{TABLE_QN}/C1"
        assert validate_asset(column, for_creation=False) == []
        assert validate_asset(column, for_creation=True) != []


# ---------------------------------------------------------------------------
# validate_transformed_dir — per-asset pass
# ---------------------------------------------------------------------------


class TestPerAssetValidation:
    def test_all_valid(self, tmp_path: Path) -> None:
        _write(tmp_path, "Database", [_database()])
        _write(tmp_path, "Schema", [_schema()])
        _write(tmp_path, "Table", [_table()])

        report = validate_transformed_dir(
            tmp_path / "transformed", check_referential_integrity=False
        )
        assert isinstance(report, AssetValidationReport)
        assert report.ok
        assert report.total == 3
        assert report.passed == 3
        assert report.failed == 0
        assert report.undeserializable == 0

    def test_invalid_and_undeserializable_counted(self, tmp_path: Path) -> None:
        bad = _table(name="BAD")
        bad.qualified_name = None  # caught by pyatlan_v9 .validate()
        _write(
            tmp_path,
            "Table",
            [_table(), bad],
            extra_lines=[b'{"typeName":"Table","attributes":'],  # truncated JSON
        )

        report = validate_transformed_dir(
            tmp_path / "transformed", check_referential_integrity=False
        )
        assert not report.ok
        assert report.total == 3
        assert report.passed == 1
        assert report.failed == 2  # one invalid + one undeserializable
        assert report.undeserializable == 1

        rendered = report.format_report()
        assert "1/3 passed" in rendered
        assert "1 invalid" in rendered
        assert "1 undeserializable" in rendered

    def test_missing_directory_is_empty_pass(self, tmp_path: Path) -> None:
        report = validate_transformed_dir(
            tmp_path / "does-not-exist", check_referential_integrity=False
        )
        assert report.ok
        assert report.total == 0

    def test_format_report_caps_listed_items_not_counts(self, tmp_path: Path) -> None:
        bad_assets = []
        for i in range(30):
            bad = _table(name=f"BAD{i}")
            bad.qualified_name = None
            bad_assets.append(bad)
        _write(tmp_path, "Table", bad_assets)

        report = validate_transformed_dir(
            tmp_path / "transformed", check_referential_integrity=False
        )
        assert report.failed == 30  # full count, not capped
        rendered = report.format_report(max_items=5)
        assert "30 invalid" in rendered
        assert "and 25 more invalid assets" in rendered

    def test_format_report_caps_undeserializable_overflow(self, tmp_path: Path) -> None:
        # Several undeserializable (truncated JSON) lines with a low max_items:
        # the undeserializable overflow line renders its own count, disjoint
        # from the invalid overflow branch.
        _write(
            tmp_path,
            "Table",
            [],
            extra_lines=[b'{"typeName":"Table","attributes":'] * 8,  # truncated JSON
        )

        report = validate_transformed_dir(
            tmp_path / "transformed", check_referential_integrity=False
        )
        assert report.undeserializable == 8
        rendered = report.format_report(max_items=3)
        assert "8 undeserializable" in rendered
        assert "and 5 more undeserializable records" in rendered


# ---------------------------------------------------------------------------
# validate_transformed_dir — referential-integrity (orphan) pass
# ---------------------------------------------------------------------------


@requires_rocksdict
class TestReferentialIntegrity:
    def test_full_hierarchy_no_orphan(self, tmp_path: Path) -> None:
        # Complete Database → Schema → Table → Column chain: every parent key is
        # present in the batch, so nothing is orphaned.
        _write(tmp_path, "Database", [_database()])
        _write(tmp_path, "Schema", [_schema()])
        _write(tmp_path, "Table", [_table()])
        _write(tmp_path, "Column", [_column("C1", TABLE_QN)])

        report = validate_transformed_dir(tmp_path / "transformed")
        assert report.orphans == []
        assert report.ok

    def test_absent_parent_is_orphan(self, tmp_path: Path) -> None:
        # Full hierarchy present, but the Column points at a Table that was never
        # emitted — the classic orphan-child case (CONNECT-292).
        missing_parent_qn = f"{SCHEMA_QN}/T_MISSING"
        _write(tmp_path, "Database", [_database()])
        _write(tmp_path, "Schema", [_schema()])
        _write(tmp_path, "Table", [_table()])  # T1 present, T_MISSING not
        _write(tmp_path, "Column", [_column("C2", missing_parent_qn)])

        report = validate_transformed_dir(tmp_path / "transformed")
        assert len(report.orphans) == 1
        orphan = report.orphans[0]
        assert isinstance(orphan, ReferentialFailure)
        assert orphan.missing_type_name == "Table"
        assert orphan.missing_qualified_name == missing_parent_qn
        assert orphan.reference_count == 1
        # The representative referencing asset is the orphaned Column.
        assert orphan.type_name == "Column"
        assert orphan.relationship == "table"
        assert not report.ok
        assert "ORPHAN" in report.format_report()

    def test_one_missing_parent_dedups_across_children(self, tmp_path: Path) -> None:
        # Two columns reference the same absent Table -> a single orphan entry
        # with reference_count == 2 (reported once per missing target, not per
        # child).
        missing_parent_qn = f"{SCHEMA_QN}/T_MISSING"
        _write(tmp_path, "Database", [_database()])
        _write(tmp_path, "Schema", [_schema()])
        _write(
            tmp_path,
            "Column",
            [_column("C1", missing_parent_qn), _column("C2", missing_parent_qn)],
        )

        report = validate_transformed_dir(tmp_path / "transformed")
        assert len(report.orphans) == 1
        assert report.orphans[0].missing_qualified_name == missing_parent_qn
        assert report.orphans[0].reference_count == 2

    def test_parentless_type_never_flagged(self, tmp_path: Path) -> None:
        # Database's parent (Connection) is created out of band and is not in the
        # resolver map, so it must never be reported as an orphan.
        _write(tmp_path, "Database", [_database()])

        report = validate_transformed_dir(tmp_path / "transformed")
        assert report.orphans == []

    def test_compound_key_discriminates_on_type_name(self, tmp_path: Path) -> None:
        # A View is emitted at the exact qualifiedName the Column expects its
        # *Table* parent at. Referential integrity keys on the (typeName,
        # qualifiedName) pair, so the View must NOT satisfy the Column's Table
        # parent → the Column is still an orphan.
        collider = View.creator(name="T1", schema_qualified_name=SCHEMA_QN)
        assert collider.qualified_name == TABLE_QN  # same qn, different typeName
        _write(tmp_path, "Database", [_database()])
        _write(tmp_path, "Schema", [_schema()])
        _write(tmp_path, "View", [collider])
        _write(tmp_path, "Column", [_column("C3", TABLE_QN)])

        report = validate_transformed_dir(tmp_path / "transformed")
        assert len(report.orphans) == 1
        assert report.orphans[0].type_name == "Column"
        assert report.orphans[0].missing_type_name == "Table"
        assert report.orphans[0].missing_qualified_name == TABLE_QN


# ---------------------------------------------------------------------------
# rocksdict-absent fallback (covered unconditionally, no [storage] extra needed)
# ---------------------------------------------------------------------------


class TestRocksdictAbsentFallback:
    """When SpillableDict can't be constructed (rocksdict missing), the orphan
    pass is skipped but per-asset validation must still run — tested by patching
    SpillableDict to raise ImportError."""

    def test_falls_back_to_per_asset_only(self, tmp_path: Path) -> None:
        # A full hierarchy plus an orphan Column: with the orphan pass live this
        # would report one orphan, so orphans == [] proves the pass was skipped.
        _write(tmp_path, "Database", [_database()])
        _write(tmp_path, "Schema", [_schema()])
        _write(tmp_path, "Table", [_table()])
        _write(tmp_path, "Column", [_column("C1", f"{SCHEMA_QN}/T_MISSING")])

        with patch.object(
            assets_module, "SpillableDict", side_effect=ImportError("no rocksdict")
        ):
            with patch.object(assets_module, "logger") as logger:
                report = validate_transformed_dir(
                    tmp_path / "transformed", check_referential_integrity=True
                )
                # Warned that the orphan pass was skipped. The warning is emitted
                # outside the except block (benign optional-dep condition), so it
                # carries no exc_info traceback — by design, not L004-suppressed.
                logger.warning.assert_called_once()
                assert "rocksdict" in logger.warning.call_args.args[0]
                assert "exc_info" not in logger.warning.call_args.kwargs

        # Per-asset validation still ran across every record; no orphans flagged.
        assert report.orphans == []
        assert report.total == 4
        # passed == total proves the per-asset pass actually ran (its stated
        # intent), not just that nothing failed — a silent skip would leave
        # passed at 0 while total/failed still looked clean.
        assert report.passed == 4
        assert report.failed == 0
        assert report.ok


# ---------------------------------------------------------------------------
# single-file input (the _iter_ndjson_lines file branch, exercised directly)
# ---------------------------------------------------------------------------


class TestSingleFileInput:
    def test_single_file_is_scanned(self, tmp_path: Path) -> None:
        # Point validate_transformed_dir at one NDJSON file, not a directory —
        # the file branch of _iter_ndjson_lines must scan it the same way.
        bad = _table(name="BAD")
        bad.qualified_name = None  # caught by pyatlan_v9 .validate()
        _write(tmp_path, "Table", [_table(), bad])
        entities = tmp_path / "transformed" / "Table" / "entities.json"

        report = validate_transformed_dir(entities, check_referential_integrity=False)

        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1
        assert not report.ok


# ---------------------------------------------------------------------------
# Relationship round-trip (FND-113)
# ---------------------------------------------------------------------------
#
# Connector transformed output carries relationships inside ``attributes``, not
# ``relationshipAttributes`` — see ``serialize_entity`` in the connector apps,
# which moves them there deliberately because the publish app reads them from
# that bucket. Decoding with ``<ConcreteType>.from_json`` picked them up only
# from ``relationshipAttributes``, so every relationship was silently dropped
# and every create-time parent check failed. Fleet-wide that read as ~98% of
# assets invalid when nothing was wrong with them.


def _hive_column_pair():
    """A Column with its parent relationship, in both on-disk shapes.

    Returns ``(nested, connector)`` where ``nested`` keeps relationships in
    ``relationshipAttributes`` (what pyatlan emits) and ``connector`` is the
    same record after ``serialize_entity`` moves them into ``attributes``
    (what actually lands in transformed output).
    """
    import copy
    import json

    from pyatlan_v9.model.assets.sql_related import RelatedTable

    conn = "default/hive/1698426951"
    tbl = f"{conn}/Hive/alibaba/alibaba_flags"
    col = Column(
        name="loan_id",
        qualified_name=f"{tbl}/loan_id",
        connection_qualified_name=conn,
        connector_name="hive",
        database_name="Hive",
        database_qualified_name=f"{conn}/Hive",
        schema_name="alibaba",
        schema_qualified_name=f"{conn}/Hive/alibaba",
        table_name="alibaba_flags",
        table_qualified_name=tbl,
        order=1,
    )
    col.table = RelatedTable(qualified_name=tbl, type_name="Table")

    nested = json.loads(col.to_nested_bytes())
    connector = copy.deepcopy(nested)
    rels = connector.pop("relationshipAttributes", None) or {}
    for key, value in rels.items():
        if value is not None:
            connector.setdefault("attributes", {})[key] = value
    return nested, connector


def test_relationships_in_attributes_survive_deserialization():
    """The production shape: relationships moved into ``attributes``."""
    import json

    _, connector = _hive_column_pair()
    assert "table" in connector["attributes"], "fixture must exercise the moved shape"
    assert "relationshipAttributes" not in connector

    asset = assets_module._deserialize(json.dumps(connector).encode())

    # Absent relationships decode to msgspec.UNSET, not None — name the real
    # sentinel or a regression to UNSET slips past this assertion.
    assert asset.table is not msgspec.UNSET
    assert asset.table.qualified_name.endswith("/alibaba_flags")
    assert validate_asset(asset) == []


def test_relationships_in_relationship_attributes_still_work():
    """Widening, not swapping — the unmoved shape must keep decoding."""
    import json

    nested, _ = _hive_column_pair()
    assert "table" in nested["relationshipAttributes"]

    asset = assets_module._deserialize(json.dumps(nested).encode())

    assert asset.table is not msgspec.UNSET
    assert validate_asset(asset) == []


def test_both_shapes_agree():
    """The two encodings of one asset must validate identically."""
    import json

    nested, connector = _hive_column_pair()
    a = assets_module._deserialize(json.dumps(nested).encode())
    b = assets_module._deserialize(json.dumps(connector).encode())

    assert type(a) is type(b) is Column
    assert validate_asset(a) == validate_asset(b) == []
    assert a.table.qualified_name == b.table.qualified_name


def test_concrete_type_is_still_resolved():
    """A generic Asset would silently skip every for_creation check, so the
    decoder must still return the concrete class."""
    import json

    _, connector = _hive_column_pair()
    assert type(assets_module._deserialize(json.dumps(connector).encode())) is Column


def test_missing_parent_still_fails():
    """The fix must not blunt the check — a Column with no parent at all still
    has to fail, or the validator stops being worth running."""
    import json

    _, connector = _hive_column_pair()
    connector["attributes"].pop("table")

    asset = assets_module._deserialize(json.dumps(connector).encode())
    errors = validate_asset(asset)

    assert any("table" in e for e in errors), errors


# ---------------------------------------------------------------------------
# Every failing (connector, typeName, relationship) seen in production
# ---------------------------------------------------------------------------
#
# Harvested from the `asset_validation_matrix` on the structured
# "Transformed-asset validation outcome" event, 2026-08-05..08 across the fleet.
# Each tuple is a real failure signature, not an invented one: the connector that
# emitted it, the Atlas typeName, and the relationship whose absence the error
# named. Together they cover ~41k sampled failures across 9 connectors.
#
# `UnstructuredFolder.unstructured_container` (bridge, 44 samples) is deliberately
# absent — that field does not exist on the pinned pyatlan_v9, so pinning it here
# would assert against a model we do not ship.

PROD_RELATIONSHIP_SIGNATURES = [
    # (connector, typeName, relationship field, the EXACT error production logged)
    (
        "tableau",
        "TableauCalculatedField",
        "datasource",
        "datasource is required for creation",
    ),
    ("fivetran", "ColumnProcess", "process", "process is required for creation"),
    (
        "thoughtspot",
        "ThoughtspotColumn",
        "thoughtspot_table",
        "thoughtspot_table is required for creation",
    ),
    (
        "tableau",
        "TableauDashboardField",
        "tableau_dashboard",
        "tableau_dashboard is required for creation",
    ),
    ("tableau", "TableauDashboard", "workbook", "workbook is required for creation"),
    ("mssql", "Schema", "database", "database is required for creation"),
    ("mssql", "Procedure", "atlan_schema", "atlan_schema is required for creation"),
    ("athena", "Table", "atlan_schema", "atlan_schema is required for creation"),
    (
        "athena",
        "Column",
        "table",
        "one of table, table_partition, view, materialised_view is required for creation",
    ),
    (
        "hive",
        "Column",
        "table",
        "one of table, table_partition, view, materialised_view is required for creation",
    ),
    ("gcs", "GCSObject", "gcs_bucket", "gcs_bucket is required for creation"),
    ("fivetran", "SalesforceField", "object", "object is required for creation"),
    (
        "fivetran",
        "SalesforceObject",
        "organization",
        "organization is required for creation",
    ),
    (
        "cosmos",
        "CosmosMongoDBCollection",
        "cosmos_mongo_db_database",
        "cosmos_mongo_db_database is required for creation",
    ),
]


def _related_class(cls: type, field_name: str) -> type:
    """The concrete Related* struct a relationship field accepts.

    Recurses through the annotation so ``Optional[list[RelatedX]]`` resolves to
    ``RelatedX``, not ``list`` — scalar unions are all the matrix carries today,
    but a list-typed relationship would silently resolve wrong without this.
    """
    import typing

    def _first_concrete(annotation) -> type | None:
        for arg in typing.get_args(annotation):
            if arg is type(None) or arg is msgspec.UnsetType:
                continue
            if isinstance(arg, type):
                return arg
            nested = _first_concrete(arg)
            if nested is not None:
                return nested
        return None

    for f in msgspec.structs.fields(cls):
        if f.name == field_name:
            resolved = f.type if isinstance(f.type, type) else _first_concrete(f.type)
            if resolved is not None:
                return resolved
    raise AssertionError(f"{cls.__name__} has no relationship field {field_name!r}")


def _as_connector_writes_it(asset) -> bytes:
    """Serialize the way connector apps do — relationships moved into
    ``attributes``. Mirrors ``serialize_entity`` in the connector apps."""
    import json

    data = json.loads(asset.to_nested_bytes())
    rels = data.pop("relationshipAttributes", None) or {}
    for key, value in rels.items():
        if value is not None:
            data.setdefault("attributes", {})[key] = value
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    ("connector", "type_name", "relationship", "prod_error"),
    PROD_RELATIONSHIP_SIGNATURES,
    ids=[f"{c}-{t}.{r}" for c, t, r, _ in PROD_RELATIONSHIP_SIGNATURES],
)
def test_prod_relationship_signature_survives_the_move(
    connector: str, type_name: str, relationship: str, prod_error: str
):
    """For every relationship production reported missing: it IS in the payload,
    the old strict decode dropped it, and this decode keeps it.

    Asserting the old decode drops it is the point — without that half, the test
    would pass on unfixed code and prove nothing.

    The assertion is on the exact message production logged, not a substring of
    the field name: setting ``table`` activates companion checks that also
    mention "table" (``table_name is required for creation``), so a looser
    check would report a fix that had not happened.
    """
    from pyatlan_v9.model.transform import get_type

    cls = get_type(type_name)
    related_cls = _related_class(cls, relationship)

    parent_qn = f"default/{connector}/1700000000/parent"
    asset = cls(name="thing", qualified_name=f"{parent_qn}/thing")
    setattr(asset, relationship, related_cls(qualified_name=parent_qn))

    raw = _as_connector_writes_it(asset)

    # Old behaviour: the strict nested decoder cannot see the relationship, and
    # reproduces the exact production error.
    dropped = cls.from_json(raw)
    assert getattr(dropped, relationship) is msgspec.UNSET, (
        f"{type_name}.{relationship} survived the old decode — "
        "this test no longer reproduces the bug"
    )
    before = validate_asset(dropped)
    assert any(
        prod_error in e for e in before
    ), f"expected production error {prod_error!r}, got {before}"

    # Fixed behaviour: relationship read back, that error gone.
    decoded = assets_module._deserialize(raw)
    assert type(decoded) is cls
    assert getattr(decoded, relationship) is not msgspec.UNSET
    assert getattr(decoded, relationship).qualified_name == parent_qn
    after = validate_asset(decoded)
    assert not any(
        prod_error in e for e in after
    ), f"{prod_error!r} still reported after the fix: {after}"


def test_prod_signature_matrix_is_not_empty():
    """Guard against the parametrize list being emptied and the suite going
    quietly green."""
    assert len(PROD_RELATIONSHIP_SIGNATURES) >= 14
    assert len({t for _, t, _, _ in PROD_RELATIONSHIP_SIGNATURES}) >= 12
    assert len({c for c, _, _, _ in PROD_RELATIONSHIP_SIGNATURES}) >= 8


def test_scalar_gaps_are_not_claimed_to_be_fixed():
    """Some production failures name plain attributes, not relationships —
    e.g. fivetran Column also reports `schema_name` / `database_name` /
    `order` missing. Those are real gaps in the transformed data and this fix
    does NOT address them. Pinned so nobody reads the fix as clearing every
    failure on the board.
    """
    from pyatlan_v9.model.assets.sql_related import RelatedTable
    from pyatlan_v9.model.transform import get_type

    cls = get_type("Column")
    col = cls(name="c", qualified_name="default/fivetran/1700000000/t/c")
    col.table = RelatedTable(qualified_name="default/fivetran/1700000000/t")

    decoded = assets_module._deserialize(_as_connector_writes_it(col))
    errors = validate_asset(decoded)

    # The relationship error is gone...
    assert not any("one of table" in e for e in errors), errors
    # ...but the missing scalars are still correctly reported.
    assert any("schema_name is required" in e for e in errors), errors
    assert any("database_name is required" in e for e in errors), errors
