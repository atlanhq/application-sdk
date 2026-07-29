import textwrap
from unittest.mock import mock_open, patch

import pyarrow as pa
import pytest

from application_sdk.transformers.common.utils import flatten_yaml_columns
from application_sdk.transformers.query import QueryBasedTransformer
from application_sdk.transformers.query.errors import (
    BuildStructLevelRequiredError,
    BuildStructPrefixRequiredError,
    IncompatibleDefaultTypeError,
)


@pytest.fixture
def sql_transformer():
    return QueryBasedTransformer(
        connector_name="test_connector", tenant_id="test_tenant"
    )


@pytest.fixture
def sample_dataframe():
    return pa.Table.from_pydict(
        {
            "table_name": ["table1", "table2"],
            "table_catalog": ["db1", "db2"],
            "table_schema": ["schema1", "schema2"],
            "connection_qualified_name": ["conn1", "conn2"],
            "table_type": ["TABLE", "VIEW"],
            "table_kind": ["r", "v"],
            "is_partition": [True, False],
            "parent_table_name": ["parent1", None],
            "partition_strategy": ["strategy1", None],
            "view_definition": ["SELECT * FROM table1", "SELECT * FROM table2"],
        }
    )


@pytest.fixture
def sample_yaml_template():
    return {
        "columns": {
            "attributes": {
                # Direct column example
                "name": {"source_query": "table_name"},
                # SQL Query example with concat method
                "qualifiedName": {
                    "source_query": "concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name)",
                    "source_columns": [
                        "connection_qualified_name",
                        "table_catalog",
                        "table_schema",
                        "table_name",
                    ],
                },
                # SQL Query example with case when
                "type": {
                    "source_query": "case when table_type = 'TABLE' then 'table' when table_type = 'VIEW' then 'view' else table_type end",
                    "source_columns": ["table_type"],
                },
                # Literal value example
                "literal": {"source_query": "'Database'"},
            }
        }
    }


# Unit Tests for Individual Methods
def test_quote_column_name(sql_transformer):
    """Test the quote_column_name method"""
    assert sql_transformer.quote_column_name("normal_column") == "normal_column"
    assert sql_transformer.quote_column_name("column.with.dots") == '"column.with.dots"'


def test_convert_to_sql_expression(sql_transformer):
    """Test the convert_to_sql_expression method"""
    column = {"name": "test.column", "source_query": "source_column"}
    result = sql_transformer.convert_to_sql_expression(column)
    assert result == 'source_column AS "test.column"'


def test_convert_to_sql_expression_with_literal(sql_transformer):
    """Test the convert_to_sql_expression method with literal=True"""
    column = {
        "name": "test.column",
        "source_query": "'Database'",  # testing the literal value
    }
    result = sql_transformer.convert_to_sql_expression(column, is_literal=True)
    assert result == '"test.column" AS "test.column"'


def test_get_sql_column_expressions(
    sql_transformer, sample_dataframe, sample_yaml_template
):
    """Test the get_sql_column_expressions method"""
    default_attributes = {}
    sample_yaml_template["columns"] = flatten_yaml_columns(
        sample_yaml_template["columns"]
    )
    columns, literal_columns = sql_transformer.get_sql_column_expressions(
        sample_yaml_template, sample_dataframe, default_attributes
    )
    assert len(columns) == 4
    assert len(literal_columns) == 1
    assert 'table_name AS "attributes.name"' in columns
    assert (
        "concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name) AS \"attributes.qualifiedName\""
        in columns
    )
    assert (
        "case when table_type = 'TABLE' then 'table' when table_type = 'VIEW' then 'view' else table_type end AS \"attributes.type\""
        in columns
    )
    assert '"attributes.literal" AS "attributes.literal"' in columns
    assert {
        "name": '"attributes.literal"',
        "source_query": "'Database'",
    } == literal_columns[0]


@patch("builtins.open", new_callable=mock_open)
@patch("yaml.safe_load")
def test_generate_sql_query(
    mock_yaml_load, mock_file, sql_transformer, sample_dataframe, sample_yaml_template
):
    """Test the generate_sql_query method"""
    mock_yaml_load.return_value = sample_yaml_template
    default_attributes = {}
    result, literal_columns = sql_transformer.generate_sql_query(
        "dummy_path", sample_dataframe, default_attributes
    )

    assert len(literal_columns) == 1
    assert {
        "name": '"attributes.literal"',
        "source_query": "'Database'",
    } == literal_columns[0]

    expected_result = textwrap.dedent(
        """\n            SELECT\n                table_name AS "attributes.name",concat(connection_qualified_name, \'/\', table_catalog, \'/\', table_schema, \'/\', table_name) AS "attributes.qualifiedName",case when table_type = \'TABLE\' then \'table\' when table_type = \'VIEW\' then \'view\' else table_type end AS "attributes.type","attributes.literal" AS "attributes.literal"\n            FROM dataframe\n            """
    )
    assert result == expected_result


def test_build_struct_with_none_level(sql_transformer):
    """Test the _build_struct method raises ValueError when level is None"""
    with pytest.raises(BuildStructLevelRequiredError):
        sql_transformer._build_struct(level=None, prefix="test")


def test_build_struct_with_none_prefix(sql_transformer):
    """Test the _build_struct method raises ValueError when prefix is None"""
    level = {
        "columns": [
            ("attributes.name", "name"),
            ("attributes.qualifiedName", "qualifiedName"),
        ],
    }
    with pytest.raises(BuildStructPrefixRequiredError):
        sql_transformer._build_struct(level=level, prefix=None)


def test_get_grouped_dataframe_by_prefix(sql_transformer):
    """
    Test the get_grouped_dataframe_by_prefix method
    and validate the nested structure of the returned list of dicts.
    """
    table = pa.Table.from_pydict(
        {
            "attributes.name": ["table1", "table2", "table3"],
            "attributes.qualifiedName": [
                "conn1/db1/schema1/table1",
                "conn1/db1/schema2/table2",
                "conn1/db1/schema3/table3",
            ],
            "attributes.database.typeName": ["Database", "Database", "Database"],
            "attributes.database.uniqueAttributes.qualifiedName": [
                "conn1/db1",
                "conn1/db1",
                "conn1/db1",
            ],
            "customAttributes.parent_name": ["parent1", None, None],
            "attributes.type": ["TABLE", "TABLE", "TABLE"],
            "attributes.kind": ["r", "r", "r"],
            "attributes.isPartition": [True, False, False],
            "attributes.partitionStrategy": ["strategy1", None, None],
            "attributes.viewDefinition": ["SELECT * FROM table1", None, None],
            "typeName": ["Table", "Table", "Table"],
            "status": ["ACTIVE", "ACTIVE", "ACTIVE"],
        }
    )

    result = sql_transformer.get_grouped_dataframe_by_prefix(table)
    assert len(result) == 3
    # Standalone columns are preserved
    assert result[0]["typeName"] == "Table"
    assert result[0]["status"] == "ACTIVE"
    # Dot-notation columns become nested dicts
    assert result[0]["attributes"]["name"] == "table1"
    assert result[0]["attributes"]["qualifiedName"] == "conn1/db1/schema1/table1"
    assert result[0]["attributes"]["type"] == "TABLE"
    assert result[0]["attributes"]["isPartition"] is True
    # Deep nesting works
    assert result[0]["attributes"]["database"]["typeName"] == "Database"
    assert (
        result[0]["attributes"]["database"]["uniqueAttributes"]["qualifiedName"]
        == "conn1/db1"
    )
    # None scalar leaf values are preserved (only all-None dicts collapse to None)
    assert result[1]["attributes"]["partitionStrategy"] is None
    assert result[0]["customAttributes"]["parent_name"] == "parent1"


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_and_attributes(
    mock_generate, sql_transformer, sample_dataframe
):
    """Test the prepare_template_and_attributes method"""
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    workflow_id = "test_workflow"
    workflow_run_id = "test_run"
    connection_qualified_name = "default/postgres/1746717318"
    connection_name = "test_conn"

    result_df, sql_template = sql_transformer.prepare_template_and_attributes(
        sample_dataframe,
        workflow_id,
        workflow_run_id,
        connection_qualified_name,
        connection_name,
        "dummy_path",
    )

    assert "connection_qualified_name" in result_df.schema.names
    assert "connection_name" in result_df.schema.names
    assert "tenant_id" in result_df.schema.names
    assert "last_sync_workflow_name" in result_df.schema.names
    assert "last_sync_run" in result_df.schema.names
    assert "last_sync_run_at" in result_df.schema.names
    assert "connector_name" in result_df.schema.names


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_passes_null_typed_columns_through(
    mock_generate, sql_transformer
):
    """Null-typed columns are passed through unchanged.

    DuckDB handles null-typed columns in SUBSTRING / REGEXP_REPLACE / CASE-WHEN
    without any pre-promotion, so prepare_template_and_attributes leaves the
    pyarrow schema as-is.
    """
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict(
        {"name": ["a"], "remarks": pa.array([None], type=pa.null())}
    )
    assert pa.types.is_null(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    assert pa.types.is_null(result_df.schema.field("remarks").type)
    assert pa.types.is_string(
        result_df.schema.field("name").type
    ) or pa.types.is_large_string(result_df.schema.field("name").type)


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_leaves_non_null_dtypes_untouched(
    mock_generate, sql_transformer
):
    """Only Null-typed columns get cast; other dtypes pass through unchanged."""
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict(
        {
            "name": ["a", "b"],
            "rows": [1, 2],
            "flag": [True, False],
            "remarks": pa.array([None, None], type=pa.null()),
        }
    )
    assert pa.types.is_integer(df.schema.field("rows").type)
    assert pa.types.is_boolean(df.schema.field("flag").type)
    assert pa.types.is_null(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    assert pa.types.is_integer(result_df.schema.field("rows").type)
    assert pa.types.is_boolean(result_df.schema.field("flag").type)
    assert pa.types.is_null(result_df.schema.field("remarks").type)


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_no_null_columns_is_noop(mock_generate, sql_transformer):
    """When no column is Null-typed, the cast path is a no-op."""
    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict({"name": ["a"], "remarks": ["r"]})
    assert pa.types.is_string(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    assert pa.types.is_string(result_df.schema.field("remarks").type)


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_enables_utf8_sql_on_null_column(
    mock_generate, sql_transformer
):
    """After the cast, DuckDB with utf8 functions on the formerly-Null
    column succeeds — the SUBSTRING / CASE-WHEN-IS-NOT-NULL patterns work after promotion."""
    import duckdb

    mock_generate.return_value = ("SELECT * FROM dataframe", None)
    df = pa.Table.from_pydict(
        {"name": ["a", "b"], "remarks": pa.array([None, None], type=pa.null())}
    )
    assert pa.types.is_null(df.schema.field("remarks").type)

    result_df, _ = sql_transformer.prepare_template_and_attributes(
        df, "wf", "wf-run", "default/pg/1", "test", "dummy_path"
    )

    conn = duckdb.connect(":memory:")
    conn.register("daft_table", result_df)
    out = (
        conn.execute(
            "SELECT name, "
            "SUBSTRING(remarks, 1, 5) AS sub, "
            "CASE WHEN remarks IS NOT NULL THEN SUBSTRING(remarks, 1, 5) ELSE '' END AS guarded "
            "FROM daft_table"
        )
        .fetch_arrow_table()
        .to_pydict()
    )
    conn.close()

    assert out["name"] == ["a", "b"]
    assert out["sub"] == [None, None]
    assert out["guarded"] == ["", ""]


def test_transform_metadata_empty_dataframe(sql_transformer):
    """Test transform_metadata with empty input returns None"""
    empty_df = pa.Table.from_pydict({"dummy": pa.array([], type=pa.string())})
    result = sql_transformer.transform_metadata(
        "TABLE", empty_df, "test_workflow", "test_run"
    )
    assert result is None


@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.prepare_template_and_attributes"
)
@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.get_grouped_dataframe_by_prefix"
)
def test_transform_metadata_list_input_unifies_keys_across_records(
    mock_group, mock_prepare, sql_transformer
):
    """Keys absent from the first record must not be dropped from the batch.

    pa.Table.from_pylist infers the schema from the first record only, so a
    naive coercion silently loses any column the first record lacks.
    """
    records = [
        {"table_name": "table1"},
        {"table_name": "table2", "view_definition": "SELECT 1"},
    ]
    mock_prepare.side_effect = lambda dataframe, *a, **k: (
        dataframe,
        "SELECT * FROM dataframe",
    )
    mock_group.return_value = [{"typeName": "Table"}]

    sql_transformer.transform_metadata("TABLE", records, "test_workflow", "test_run")

    coerced = mock_prepare.call_args.args[0]
    assert coerced.schema.names == ["table_name", "view_definition"]
    assert coerced.to_pylist()[0]["view_definition"] is None
    assert coerced.to_pylist()[1]["view_definition"] == "SELECT 1"


LITERAL_STATUS_TEMPLATE = textwrap.dedent("""
columns:
  status:
    source_query: "'ACTIVE'"
  attributes:
    name:
      source_query: table_name
""")


def test_template_literal_wins_when_colliding_column_is_all_null(
    sql_transformer, tmp_path
):
    """Records carrying only explicit Nones for a literal-declared name must
    still get the template literal, not a null column."""
    template = tmp_path / "table.yaml"
    template.write_text(LITERAL_STATUS_TEMPLATE)
    records = [
        {"table_name": "t1", "status": None},
        {"table_name": "t2", "status": None},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["status"] for row in result] == ["ACTIVE", "ACTIVE"]


def test_literal_precedence_is_row_local_on_colliding_column(sql_transformer, tmp_path):
    """A genuine source value wins for its own row; rows without one get the
    template literal. Row-local, so the outcome is independent of batch
    membership and record order. Collisions still belong fixed at the
    extractor."""
    template = tmp_path / "table.yaml"
    template.write_text(LITERAL_STATUS_TEMPLATE)
    records = [
        {"table_name": "t1"},
        {"table_name": "t2", "status": "DELETED"},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["status"] for row in result] == ["ACTIVE", "DELETED"]


def test_castable_default_is_coerced_to_the_column_type(sql_transformer, tmp_path):
    """An int template literal colliding with a string source column is cast
    to the column's type and filled row-locally ('0', not a crash). Lossy but
    representable casts are accepted deliberately; only unrepresentable
    combinations raise (see the list-column test below)."""
    template = tmp_path / "table.yaml"
    template.write_text(
        textwrap.dedent("""
        columns:
          status:
            source_query: 0
          attributes:
            name:
              source_query: table_name
        """)
    )
    records = [
        {"table_name": "t1", "status": None},
        {"table_name": "t2", "status": "OK"},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["status"] for row in result] == ["0", "OK"]


@pytest.mark.parametrize(
    "status_values",
    [
        pytest.param([None, ["a", "b"]], id="partially-null"),
        pytest.param([None, None], id="all-null"),
    ],
)
def test_uncastable_default_raises_regardless_of_null_shape(
    sql_transformer, tmp_path, status_values
):
    """A string literal cannot fill a list-typed colliding column: pyarrow's
    fill_null would explode 'ACTIVE' into single characters silently. The
    guard must raise a typed error naming the column — and the answer must
    not depend on how many of the column's values happen to be null in this
    batch."""
    template = tmp_path / "table.yaml"
    template.write_text(LITERAL_STATUS_TEMPLATE)
    table = pa.table(
        {
            "table_name": ["t1", "t2"],
            "status": pa.array(status_values, type=pa.list_(pa.string())),
        }
    )

    with pytest.raises(IncompatibleDefaultTypeError) as exc_info:
        sql_transformer.transform_metadata(
            "TABLE",
            table,
            "wf",
            "run",
            entity_class_definitions={"TABLE": str(template)},
            connection_qualified_name="default/snowflake/1",
        )

    assert "status" in str(exc_info.value)


def test_compatible_default_fills_typed_all_null_column_preserving_type(
    sql_transformer, tmp_path
):
    """A castable default filling a typed all-null column must keep the
    column's Arrow type, not re-infer its own."""
    template = tmp_path / "table.yaml"
    template.write_text(
        textwrap.dedent("""
        columns:
          status:
            source_query: 0
          attributes:
            name:
              source_query: table_name
        """)
    )
    table = pa.table(
        {
            "table_name": ["t1", "t2"],
            "status": pa.array([None, None], type=pa.string()),
        }
    )

    prepared, _ = sql_transformer.prepare_template_and_attributes(
        table,
        "wf",
        "run",
        connection_qualified_name="default/snowflake/1",
        entity_sql_template_path=str(template),
    )

    assert prepared.column("status").type == pa.string()
    assert prepared.column("status").to_pylist() == ["0", "0"]


def test_reserved_default_fills_null_rows_of_colliding_column(
    sql_transformer, tmp_path
):
    """Reserved defaults (connection_qualified_name etc.) follow the same
    row-local rule as template literals when records carry a colliding key."""
    template = tmp_path / "table.yaml"
    template.write_text(
        textwrap.dedent("""
        columns:
          attributes:
            qualifiedName:
              source_query: concat(connection_qualified_name, '/', table_name)
              source_columns: [connection_qualified_name, table_name]
        """)
    )
    records = [
        {"table_name": "t1", "connection_qualified_name": None},
        {"table_name": "t2", "connection_qualified_name": "custom/cqn/2"},
    ]

    result = sql_transformer.transform_metadata(
        "TABLE",
        records,
        "wf",
        "run",
        entity_class_definitions={"TABLE": str(template)},
        connection_qualified_name="default/snowflake/1",
    )

    assert [row["attributes"]["qualifiedName"] for row in result] == [
        "default/snowflake/1/t1",
        "custom/cqn/2/t2",
    ]


@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.prepare_template_and_attributes"
)
@patch(
    "application_sdk.transformers.query.QueryBasedTransformer.get_grouped_dataframe_by_prefix"
)
def test_transform_metadata(
    mock_group, mock_prepare, sql_transformer, sample_dataframe
):
    """Test the transform_metadata method"""
    mock_prepare.return_value = (sample_dataframe, "SELECT * FROM dataframe")
    mock_group.return_value = [{"typeName": "Table"}]

    result = sql_transformer.transform_metadata(
        "TABLE",
        sample_dataframe,
        "test_workflow",
        "test_run",
        connection_qualified_name="test_connection",
    )

    assert result is not None
    mock_prepare.assert_called_once()
    mock_group.assert_called_once()
