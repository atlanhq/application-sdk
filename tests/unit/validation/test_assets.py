"""Tests for application_sdk.validation.assets.

Fixtures are built from real pyatlan_v9 asset creators and serialized with
``to_nested_bytes()`` — the same wire shape connectors write to
``transformed/<Entity>/entities.json`` — so the read-back path is exercised
exactly as it runs in production.

The referential-integrity (orphan) tests need the ``rocksdict``-backed
``SpillableDict`` from the ``[storage]`` extra and are skipped without it — run
``uv sync --extra storage`` (CI uses ``--all-extras``) for full local coverage.
The rocksdict-absent fallback itself is covered unconditionally in
``TestRocksdictAbsentFallback`` by patching ``SpillableDict`` to raise.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

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
        assert isinstance(validate_asset(table), list)

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
        assert "invalid" in rendered

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
        assert orphan.type_name == "Column"
        assert orphan.missing_parent_type_name == "Table"
        assert orphan.missing_parent_qualified_name == missing_parent_qn
        assert not report.ok
        assert "ORPHAN" in report.format_report()

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
        assert report.orphans[0].missing_parent_type_name == "Table"
        assert report.orphans[0].missing_parent_qualified_name == TABLE_QN


# ---------------------------------------------------------------------------
# validate_transformed_dir — rocksdict-absent fallback (no [storage] extra)
# ---------------------------------------------------------------------------


class TestRocksdictAbsentFallback:
    """When SpillableDict can't be constructed (rocksdict missing), the orphan
    pass is skipped but per-asset validation must still run — unconditionally
    tested by patching SpillableDict to raise ImportError."""

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
                # Warned about the skipped orphan pass, with the traceback.
                logger.warning.assert_called_once()
                assert logger.warning.call_args.kwargs.get("exc_info") is True

        # Per-asset validation still ran across every record; no orphans flagged.
        assert report.orphans == []
        assert report.total == 4
        assert report.failed == 0
        assert report.ok
