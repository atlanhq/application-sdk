"""Tests for the fixture kit's output-schema assertion (FND-5007).

Covers the declaration types, the value rules on both formats, the report, and the
pandera-YAML reader — including the postgres connector's transformed schemas, read
from a copy under ``data/`` and run against representative output, which is the
migration path off ``validate_with_pandera``.
"""

from __future__ import annotations

import copy
import subprocess
import sys
import textwrap
from pathlib import Path

import orjson
import pytest

from application_sdk.testing.integration._errors import OutputSchemaDeclarationError
from application_sdk.testing.integration.output_schema import (
    FieldRule,
    OutputSchema,
    TypenameReport,
    assert_output_matches,
    load_pandera_schemas,
    validate_output,
)

POSTGRES_SCHEMAS = Path(__file__).parent / "data" / "pandera_postgres_transformed"
CONNECTION_QN = "default/postgres/1700000000"


def _write_ndjson(root: Path, typename: str, records: list[object]) -> Path:
    directory = root / "transformed" / typename
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "part-0.json").write_bytes(
        b"".join(orjson.dumps(r) + b"\n" for r in records)
    )
    return root


def _only(report_typenames: tuple[TypenameReport, ...]) -> TypenameReport:
    assert len(report_typenames) == 1
    return report_typenames[0]


def _rules(report: TypenameReport) -> set[tuple[str, str]]:
    return {(f.field, f.rule) for f in report.failures}


TABLE = OutputSchema(
    typename="table",
    min_records=1,
    fields=(
        FieldRule(
            "typeName",
            type="string",
            nullable=False,
            allowed=("Table", "View", "MaterialisedView"),
        ),
        FieldRule(
            "attributes.qualifiedName",
            type="string",
            nullable=False,
            pattern=r"[^/]+/[^/]+/[^/]+/[^/]+/[^/]+/[^/]+",
        ),
    ),
)


def _table(qn: str = f"{CONNECTION_QN}/db/sch/t1", type_name: str = "Table") -> dict:
    return {"typeName": type_name, "attributes": {"qualifiedName": qn}}


# ---------------------------------------------------------------------------
# Red-green: the two failures the issue names
# ---------------------------------------------------------------------------


class TestRedGreen:
    def test_good_output_passes(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "table", [_table(), _table(type_name="View")])
        report = assert_output_matches(tmp_path, [TABLE])
        assert report.ok
        assert report.typenames[0].records == 2

    def test_bad_qualified_name_fails(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "table", [_table(), _table(qn="default/postgres/t1")])
        with pytest.raises(AssertionError) as raised:
            assert_output_matches(tmp_path, [TABLE])
        message = str(raised.value)
        assert "[FAIL] table" in message
        assert "attributes.qualifiedName: pattern x1" in message
        assert "'default/postgres/t1'" in message
        assert "part-0.json:2" in message

    def test_disallowed_type_name_fails(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "table", [_table(type_name="Column")])
        report = validate_output(tmp_path, [TABLE])
        assert not report.ok
        assert _rules(_only(report.typenames)) == {("typeName", "allowed")}


# ---------------------------------------------------------------------------
# Value rules, NDJSON
# ---------------------------------------------------------------------------


class TestNdjsonRules:
    def test_missing_required_key_is_reported_once(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "table", [{"attributes": {"qualifiedName": "a"}}])
        schema = OutputSchema("table", (FieldRule("typeName", nullable=False),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert _rules(report) == {("typeName", "missing")}

    def test_optional_key_may_be_absent(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "table", [{"a": 1}])
        schema = OutputSchema(
            "table", (FieldRule("b", required=False, nullable=False, minimum=0),)
        )
        assert validate_output(tmp_path, [schema]).ok

    def test_null_fails_only_when_non_nullable(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "table", [{"a": None, "b": None}])
        schema = OutputSchema(
            "table",
            (
                FieldRule("a", type="string", nullable=False),
                FieldRule("b", type="string", allowed=("x",), pattern="x"),
            ),
        )
        assert _rules(_only(validate_output(tmp_path, [schema]).typenames)) == {
            ("a", "null")
        }

    def test_type_mismatch_comes_from_the_adr_0020_validator(
        self, tmp_path: Path
    ) -> None:
        _write_ndjson(tmp_path, "table", [{"n": "1"}, {"n": True}, {"n": 2}])
        schema = OutputSchema("table", (FieldRule("n", type="int"),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert _rules(report) == {("n", "type")}
        assert report.failures[0].count == 2

    def test_pattern_matches_the_whole_value(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"s": "ACTIVE"}, {"s": "ACTIVE_X"}])
        schema = OutputSchema("t", (FieldRule("s", pattern="ACTIVE"),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert report.failures[0].count == 1
        assert "'ACTIVE_X'" in report.failures[0].examples[0].message

    def test_pattern_on_a_non_string_fails(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"s": 5}])
        schema = OutputSchema("t", (FieldRule("s", pattern=".+"),))
        assert _rules(_only(validate_output(tmp_path, [schema]).typenames)) == {
            ("s", "pattern")
        }

    def test_allowed_keeps_bool_and_int_apart(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"v": True}, {"v": 1}, {"v": 1.0}])
        schema = OutputSchema("t", (FieldRule("v", allowed=(1,)),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert report.failures[0].count == 1
        assert "True" in report.failures[0].examples[0].message

    def test_length_bounds(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"s": ""}, {"s": "abcd"}, {"s": "ab"}])
        schema = OutputSchema("t", (FieldRule("s", min_length=1, max_length=3),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert report.failures[0].rule == "length"
        assert report.failures[0].count == 2

    def test_range_bounds_inclusive_and_exclusive(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"n": 0}, {"n": 1}, {"n": 10}, {"n": 11}])
        inclusive = OutputSchema("t", (FieldRule("n", minimum=0, maximum=10),))
        exclusive = OutputSchema(
            "t", (FieldRule("n", exclusive_minimum=0, exclusive_maximum=11),)
        )
        assert (
            validate_output(tmp_path, [inclusive]).typenames[0].failures[0].count == 1
        )
        assert (
            validate_output(tmp_path, [exclusive]).typenames[0].failures[0].count == 2
        )

    def test_unique(self, tmp_path: Path) -> None:
        _write_ndjson(
            tmp_path,
            "t",
            [{"q": "a"}, {"q": "b"}, {"q": "a"}, {"q": None}, {"q": None}],
        )
        schema = OutputSchema("t", (FieldRule("q", unique=True),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert _rules(report) == {("q", "unique")}
        assert report.failures[0].count == 1

    def test_element_paths_check_every_element(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"cols": [{"name": "a"}, {"name": ""}]}])
        schema = OutputSchema("t", (FieldRule("cols[].name", min_length=1),))
        assert _rules(_only(validate_output(tmp_path, [schema]).typenames)) == {
            ("cols[].name", "length")
        }

    def test_record_count_bounds(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"a": 1}, {"a": 2}])
        assert not validate_output(tmp_path, [OutputSchema("t", min_records=3)]).ok
        assert not validate_output(tmp_path, [OutputSchema("t", max_records=1)]).ok
        assert validate_output(
            tmp_path, [OutputSchema("t", min_records=2, max_records=2)]
        ).ok

    def test_examples_are_capped_but_counts_are_exact(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"a": "bad"} for _ in range(50)])
        schema = OutputSchema("t", (FieldRule("a", allowed=("good",)),))
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert report.failures[0].count == 50
        assert len(report.failures[0].examples) == 5
        assert "... and 45 more" in report.format_report()

    def test_undecodable_line_fails_with_or_without_fields(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "transformed" / "t"
        directory.mkdir(parents=True)
        (directory / "part.json").write_bytes(b'{"a": 1}\n{not json\n')
        for schema in (OutputSchema("t"), OutputSchema("t", (FieldRule("a"),))):
            report = _only(validate_output(tmp_path, [schema]).typenames)
            assert ("", "undecodable") in _rules(report)


# ---------------------------------------------------------------------------
# Whole-artifact outcomes
# ---------------------------------------------------------------------------


class TestArtifactOutcomes:
    @pytest.mark.parametrize(
        "schema",
        [
            OutputSchema("missing"),
            OutputSchema("missing", (FieldRule("a"),), min_records=1),
        ],
    )
    def test_absent_output_fails_once(
        self, tmp_path: Path, schema: OutputSchema
    ) -> None:
        report = _only(validate_output(tmp_path, [schema]).typenames)
        assert _rules(report) == {("", "absent")}

    def test_empty_ndjson_is_absent_not_a_pass(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [])
        report = _only(validate_output(tmp_path, [OutputSchema("t")]).typenames)
        assert _rules(report) == {("", "absent")}

    def test_mixed_formats_must_be_declared(self, tmp_path: Path) -> None:
        _write_ndjson(tmp_path, "t", [{"a": 1}])
        (tmp_path / "transformed" / "t" / "part.parquet").write_bytes(b"")
        report = _only(validate_output(tmp_path, [OutputSchema("t")]).typenames)
        assert _rules(report) == {("", "unsupported")}
        declared = OutputSchema("t", artifact_format="ndjson")
        assert validate_output(tmp_path, [declared]).ok

    def test_subdirectory_is_joined(self, tmp_path: Path) -> None:
        (tmp_path / "raw" / "t").mkdir(parents=True)
        (tmp_path / "raw" / "t" / "p.json").write_bytes(b'{"a": 1}\n')
        assert validate_output(tmp_path, [OutputSchema("t")], subdirectory="raw").ok
        assert validate_output(tmp_path, [OutputSchema("raw/t")], subdirectory="").ok

    def test_no_schemas_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OutputSchemaDeclarationError):
            validate_output(tmp_path, [])


# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


class TestParquet:
    @pytest.fixture
    def raw_root(self, tmp_path: Path) -> Path:
        pa = pytest.importorskip("pyarrow")
        pq = pytest.importorskip("pyarrow.parquet")
        directory = tmp_path / "raw" / "column"
        directory.mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "COLUMN_NAME": ["id", "name", ""],
                    "ORDINAL_POSITION": [1, 2, 0],
                    "DATA_TYPE": ["int", "varchar", None],
                    "UNUSED": [1.0, 2.0, 3.0],
                }
            ),
            directory / "chunk-0.parquet",
            row_group_size=1,
        )
        pq.write_table(
            pa.table(
                {
                    "COLUMN_NAME": ["id"],
                    "ORDINAL_POSITION": [3],
                    "DATA_TYPE": ["int"],
                    "UNUSED": [4.0],
                }
            ),
            directory / "chunk-1.parquet",
        )
        return tmp_path

    def test_types_values_and_count(self, raw_root: Path) -> None:
        schema = OutputSchema(
            "column",
            min_records=4,
            fields=(
                FieldRule("COLUMN_NAME", type="string", min_length=1, unique=True),
                FieldRule("ORDINAL_POSITION", type="int", exclusive_minimum=0),
                FieldRule("DATA_TYPE", type="string", nullable=False),
            ),
        )
        report = _only(
            validate_output(raw_root, [schema], subdirectory="raw").typenames
        )
        assert report.artifact_format == "parquet"
        assert report.records == 4
        assert _rules(report) == {
            ("COLUMN_NAME", "length"),
            ("COLUMN_NAME", "unique"),
            ("ORDINAL_POSITION", "range"),
            ("DATA_TYPE", "null"),
        }
        unique = next(f for f in report.failures if f.rule == "unique")
        assert unique.examples[0].file.endswith("chunk-1.parquet")
        assert unique.examples[0].line == 1
        length = next(f for f in report.failures if f.rule == "length")
        assert length.examples[0].line == 3

    def test_type_mismatch_from_the_footer(self, raw_root: Path) -> None:
        schema = OutputSchema("column", (FieldRule("ORDINAL_POSITION", type="string"),))
        report = _only(
            validate_output(raw_root, [schema], subdirectory="raw").typenames
        )
        assert _rules(report) == {("ORDINAL_POSITION", "type")}

    def test_reads_only_the_value_ruled_columns(
        self, raw_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pq = pytest.importorskip("pyarrow.parquet")
        requested: list[list[str]] = []
        original = pq.ParquetFile.iter_batches

        def spy(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            requested.append(list(kwargs.get("columns") or []))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(pq.ParquetFile, "iter_batches", spy)
        schema = OutputSchema(
            "column",
            (
                FieldRule("COLUMN_NAME", type="string"),
                FieldRule("ORDINAL_POSITION", type="int", minimum=0),
            ),
        )
        assert validate_output(raw_root, [schema], subdirectory="raw").ok
        assert requested == [["ORDINAL_POSITION"], ["ORDINAL_POSITION"]]

    def test_count_only_reads_no_rows(
        self, raw_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pq = pytest.importorskip("pyarrow.parquet")

        def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            raise AssertionError("rows were read for a count-only schema")

        monkeypatch.setattr(pq.ParquetFile, "iter_batches", refuse)
        schema = OutputSchema(
            "column", (FieldRule("UNUSED", type="float"),), min_records=4
        )
        assert validate_output(raw_root, [schema], subdirectory="raw").ok


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


class TestDeclarations:
    def test_unknown_type_is_refused(self) -> None:
        with pytest.raises(OutputSchemaDeclarationError, match="logical type"):
            FieldRule("a", type="varchar")  # type: ignore[arg-type]

    def test_bad_pattern_is_refused(self) -> None:
        with pytest.raises(OutputSchemaDeclarationError, match="does not compile"):
            FieldRule("a", pattern="(")

    def test_duplicate_path_is_refused(self) -> None:
        with pytest.raises(OutputSchemaDeclarationError, match="twice"):
            OutputSchema("t", (FieldRule("a"), FieldRule("a")))


# ---------------------------------------------------------------------------
# The pandera reader
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


class TestPanderaReader:
    def test_typename_follows_the_file_layout(self, tmp_path: Path) -> None:
        minimal = "schema_type: dataframe\ncolumns: {}\n"
        _write_yaml(tmp_path / "table.yaml", minimal)
        _write_yaml(tmp_path / "view" / "schema.yaml", minimal)
        _write_yaml(tmp_path / "raw" / "column.yml", minimal)
        names = {s.typename for s in load_pandera_schemas(tmp_path)}
        assert names == {"table", "view", "raw/column"}

    def test_translates_the_fleet_subset(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "t.yaml",
            """\
            schema_type: dataframe
            api: pandas
            version: 0.22.1
            columns:
              typeName:
                dtype: str
                nullable: false
                required: true
                checks:
                  isin:
                    value: [Table, View]
              status:
                dtype: str
                equal_to: ACTIVE
              name:
                dtype: object
                nullable: true
                required: true
                unique: true
                checks:
                  - value: 1
                    options: {check_name: str_length}
                    min_value: 1
                    max_value: 128
              size:
                dtype: int64
                required: false
                checks:
                  greater_than_or_equal_to: 0
                  less_than: {value: 100}
            checks:
              check_record_count_ge:
                value: 2
            strict: false
            coerce: false
            """,
        )
        (schema,) = load_pandera_schemas(tmp_path)
        rules = {r.path: r for r in schema.fields}
        assert schema.min_records == 2 and schema.max_records is None
        assert rules["typeName"] == FieldRule(
            "typeName", type="string", nullable=False, allowed=("Table", "View")
        )
        assert rules["status"].allowed == ("ACTIVE",)
        assert rules["name"].type == "any"
        assert rules["name"].required is False  # required+nullable: see docstring
        assert rules["name"].unique is True
        assert (rules["name"].min_length, rules["name"].max_length) == (1, 128)
        assert rules["size"].required is False
        assert (rules["size"].minimum, rules["size"].exclusive_maximum) == (0, 100)

    def test_str_matches_keeps_prefix_semantics(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "schema" / "t.yaml",
            """\
            schema_type: dataframe
            columns:
              s:
                dtype: str
                checks:
                  str_matches:
                    value: ACTIVE
            """,
        )
        _write_ndjson(tmp_path / "out", "t", [{"s": "ACTIVE"}, {"s": "ACTIVE_X"}])
        _write_ndjson(tmp_path / "bad", "t", [{"s": "INACTIVE"}])
        schemas = load_pandera_schemas(tmp_path / "schema")
        assert validate_output(tmp_path / "out", schemas).ok
        assert not validate_output(tmp_path / "bad", schemas).ok

    @pytest.mark.parametrize(
        ("body", "key"),
        [
            ("columns:\n  a:\n    checks:\n      str_contains: x\n", "str_contains"),
            ("columns:\n  a:\n    dtype: category\n", "dtype"),
            ("columns:\n  a:\n    coerce: true\n", "coerce"),
            ("strict: true\ncolumns: {}\n", "strict"),
            (
                "checks:\n  my_custom_check: {value: 1}\ncolumns: {}\n",
                "my_custom_check",
            ),
            ("schema_type: series\ncolumns: {}\n", "schema_type"),
            ("surprise: 1\ncolumns: {}\n", "surprise"),
        ],
    )
    def test_refuses_what_it_does_not_translate(
        self, tmp_path: Path, body: str, key: str
    ) -> None:
        _write_yaml(tmp_path / "t.yaml", body)
        with pytest.raises(OutputSchemaDeclarationError, match=key):
            load_pandera_schemas(tmp_path)

    def test_empty_or_missing_directory_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OutputSchemaDeclarationError, match="not found"):
            load_pandera_schemas(tmp_path / "nope")
        with pytest.raises(OutputSchemaDeclarationError, match="no pandera YAML"):
            load_pandera_schemas(tmp_path)


# ---------------------------------------------------------------------------
# Migration proof: the postgres connector's transformed schemas
# ---------------------------------------------------------------------------


def _postgres_output() -> dict[str, list[dict]]:
    """Representative postgres transformed output, one record per typename plus a view."""
    common = {
        "connectionQualifiedName": CONNECTION_QN,
        "connectionName": "postgres-it",
        "connectorName": "postgres",
        "tenantId": "default",
        "lastSyncRunAt": 1700000000000,
    }
    db_qn = f"{CONNECTION_QN}/demo_db"
    schema_qn = f"{db_qn}/public"
    table_qn = f"{schema_qn}/orders"
    return {
        "database": [
            {
                "typeName": "Database",
                "status": "ACTIVE",
                "attributes": {
                    **common,
                    "name": "demo_db",
                    "qualifiedName": db_qn,
                    "schemaCount": 1,
                },
            }
        ],
        "schema": [
            {
                "typeName": "Schema",
                "status": "ACTIVE",
                "attributes": {
                    **common,
                    "name": "public",
                    "qualifiedName": schema_qn,
                    "databaseName": "demo_db",
                    "databaseQualifiedName": db_qn,
                    "database": {
                        "typeName": "Database",
                        "uniqueAttributes": {"qualifiedName": db_qn},
                    },
                    "tableCount": 1,
                    "viewsCount": 1,
                },
            }
        ],
        "table": [
            {
                "typeName": type_name,
                "status": "ACTIVE",
                "attributes": {
                    **common,
                    "name": name,
                    "qualifiedName": f"{schema_qn}/{name}",
                    "databaseName": "demo_db",
                    "databaseQualifiedName": db_qn,
                    "schemaName": "public",
                    "schemaQualifiedName": schema_qn,
                    "columnCount": 2,
                },
                "customAttributes": {"table_type": table_type},
            }
            for type_name, name, table_type in (
                ("Table", "orders", "BASE TABLE"),
                ("View", "order_view", "VIEW"),
            )
        ],
        "column": [
            {
                "typeName": "Column",
                "status": "ACTIVE",
                "attributes": {
                    **common,
                    "name": "id",
                    "qualifiedName": f"{table_qn}/id",
                    "databaseName": "demo_db",
                    "databaseQualifiedName": db_qn,
                    "schemaName": "public",
                    "schemaQualifiedName": schema_qn,
                    "dataType": "integer",
                    "isNullable": False,
                    "isPrimary": True,
                    "isForeign": False,
                    "isPartition": False,
                    "numericScale": 0,
                },
                "customAttributes": {"type_name": "int4"},
            }
        ],
    }


def _write_postgres(root: Path, output: dict[str, list[dict]]) -> Path:
    for typename, records in output.items():
        _write_ndjson(root, typename, list(records))
    return root


class TestPostgresMigration:
    def test_every_schema_is_read(self) -> None:
        schemas = load_pandera_schemas(POSTGRES_SCHEMAS)
        assert {s.typename for s in schemas} == {
            "database",
            "schema",
            "table",
            "column",
        }
        assert all(s.min_records == 1 for s in schemas)

    def test_representative_output_passes(self, tmp_path: Path) -> None:
        run_root = _write_postgres(tmp_path, _postgres_output())
        report = assert_output_matches(run_root, load_pandera_schemas(POSTGRES_SCHEMAS))
        assert {t.typename: t.records for t in report.typenames} == {
            "column": 1,
            "database": 1,
            "schema": 1,
            "table": 2,
        }

    @pytest.mark.parametrize(
        ("typename", "mutate", "field", "rule"),
        [
            (
                "table",
                lambda r: r["attributes"].update(qualifiedName="default/postgres"),
                "attributes.qualifiedName",
                "pattern",
            ),
            ("table", lambda r: r.update(typeName="Column"), "typeName", "allowed"),
            (
                "database",
                lambda r: r["attributes"].pop("name"),
                "attributes.name",
                "missing",
            ),
            (
                "column",
                lambda r: r["attributes"].update(isPrimary="yes"),
                "attributes.isPrimary",
                "type",
            ),
            (
                "schema",
                lambda r: r["attributes"].update(tenantId=None),
                "attributes.tenantId",
                "null",
            ),
        ],
    )
    def test_each_broken_rule_is_caught(
        self, tmp_path: Path, typename, mutate, field: str, rule: str
    ) -> None:
        output = copy.deepcopy(_postgres_output())
        mutate(output[typename][0])
        run_root = _write_postgres(tmp_path, output)
        report = validate_output(run_root, load_pandera_schemas(POSTGRES_SCHEMAS))
        (failed,) = report.failed
        assert failed.typename == typename
        assert _rules(failed) == {(field, rule)}

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda o: None,
            lambda o: o["table"][0]["attributes"].update(qualifiedName="default/pg"),
            lambda o: o["table"][0].update(typeName="Column"),
            lambda o: o["database"][0]["attributes"].pop("name"),
            lambda o: o["column"][0]["attributes"].update(isPrimary="yes"),
            lambda o: o["schema"][0]["attributes"].update(tenantId=None),
            lambda o: o["schema"][0].update(status="INACTIVE"),
            lambda o: o.update(view=[]) or o["table"].clear(),
        ],
    )
    def test_verdicts_agree_with_validate_with_pandera(
        self, tmp_path: Path, mutate
    ) -> None:
        """Differential: the new path and pandera agree on every typename."""
        pytest.importorskip("pandera")
        from application_sdk.testing.integration.validation import (  # noqa: PLC0415 — needs the optional pandera extra
            validate_with_pandera,
        )

        output = copy.deepcopy(_postgres_output())
        mutate(output)
        run_root = _write_postgres(tmp_path, output)

        ours = {
            t.typename: t.ok
            for t in validate_output(
                run_root, load_pandera_schemas(POSTGRES_SCHEMAS)
            ).typenames
        }
        theirs = {
            r["entity"]: r["success"]
            for r in validate_with_pandera(str(POSTGRES_SCHEMAS), str(run_root))
        }
        assert ours == theirs


# ---------------------------------------------------------------------------
# Dependency floor
# ---------------------------------------------------------------------------


def test_import_loads_no_dataframe_library() -> None:
    script = textwrap.dedent(
        """
        import sys
        import application_sdk.testing.integration.output_schema  # noqa: F401
        heavy = {"pandas", "pandera", "pyarrow", "polars", "daft", "duckdb"}
        print(",".join(sorted(heavy & {m.split(".")[0] for m in sys.modules})))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


def test_ndjson_validation_loads_no_dataframe_library(tmp_path: Path) -> None:
    _write_ndjson(tmp_path, "t", [{"a": "x"}])
    script = textwrap.dedent(
        f"""
        import sys
        from application_sdk.testing.integration.output_schema import (
            FieldRule, OutputSchema, assert_output_matches,
        )
        assert_output_matches(
            {str(tmp_path)!r}, [OutputSchema("t", (FieldRule("a", pattern="x"),))]
        )
        heavy = {{"pandas", "pandera", "pyarrow", "polars", "daft", "duckdb"}}
        print(",".join(sorted(heavy & {{m.split(".")[0] for m in sys.modules}})))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
