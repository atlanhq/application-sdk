import textwrap
from unittest.mock import mock_open, patch

import daft
import pytest
from daft.logical.schema import Field

from application_sdk.transformers.common.utils import flatten_yaml_columns
from application_sdk.transformers.query import QueryBasedTransformer


@pytest.fixture
def sql_transformer():
    return QueryBasedTransformer(
        connector_name="test_connector", tenant_id="test_tenant"
    )


@pytest.fixture
def sample_dataframe():
    return daft.from_pydict(
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


def test_build_struct(sql_transformer):
    """Test the _build_struct method"""
    level = {
        "columns": [
            ("attributes.name", "name"),
            ("attributes.qualifiedName", "qualifiedName"),
        ],
        "nested": {"columns": [("nested.column", "column")]},
    }
    result = sql_transformer._build_struct(level, "test")
    expected_result = "if [[not(is_null(col(attributes.name))) | not(is_null(col(attributes.qualifiedName)))] | not(is_null(if [not(is_null(col(nested.column)))] then [struct(col(nested.column) as column)] else [lit(Null)] as nested))] then [struct(col(attributes.name) as name, col(attributes.qualifiedName) as qualifiedName, if [not(is_null(col(nested.column)))] then [struct(col(nested.column) as column)] else [lit(Null)] as nested)] else [lit(Null)] as test"
    assert str(result) == expected_result


def test_get_grouped_dataframe_by_prefix(sql_transformer):
    """
    Test the get_grouped_dataframe_by_prefix method
    and validate the schema of the transformed dataframe
    to make sure it follows the nesting structure
    """

    df = daft.from_pydict(
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

    result = sql_transformer.get_grouped_dataframe_by_prefix(df)
    assert result.count_rows() == 3
    expected_schema = [
        Field.create(name="typeName", dtype=daft.DataType.string()),
        Field.create(name="status", dtype=daft.DataType.string()),
        Field.create(
            name="attributes",
            dtype=daft.DataType.struct(
                {
                    "name": daft.DataType.string(),
                    "qualifiedName": daft.DataType.string(),
                    "type": daft.DataType.string(),
                    "kind": daft.DataType.string(),
                    "isPartition": daft.DataType.bool(),
                    "partitionStrategy": daft.DataType.string(),
                    "viewDefinition": daft.DataType.string(),
                    "database": daft.DataType.struct(
                        {
                            "typeName": daft.DataType.string(),
                            "uniqueAttributes": daft.DataType.struct(
                                {"qualifiedName": daft.DataType.string()}
                            ),
                        }
                    ),
                }
            ),
        ),
        Field.create(
            name="customAttributes",
            dtype=daft.DataType.struct({"parent_name": daft.DataType.string()}),
        ),
    ]
    assert expected_schema == [schema for schema in result.schema()]


@patch("application_sdk.transformers.query.QueryBasedTransformer.generate_sql_query")
def test_prepare_template_and_attributes(
    mock_generate, sql_transformer, sample_dataframe
):
    """Test the _prepare_template_and_attributes method"""
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

    assert "connection_qualified_name" in result_df.column_names
    assert "connection_name" in result_df.column_names
    assert "tenant_id" in result_df.column_names
    assert "last_sync_workflow_name" in result_df.column_names
    assert "last_sync_run" in result_df.column_names
    assert "last_sync_run_at" in result_df.column_names
    assert "connector_name" in result_df.column_names


def test_transform_metadata_empty_dataframe(sql_transformer):
    """Test transform_metadata with empty dataframe"""
    empty_df = daft.from_pydict(
        {"dummy": []}
    )  # Add a dummy column to avoid empty schema
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
def test_transform_metadata(
    mock_group, mock_prepare, sql_transformer, sample_dataframe
):
    """Test the transform_metadata method"""
    mock_prepare.return_value = (sample_dataframe, "SELECT * FROM dataframe")
    mock_group.return_value = sample_dataframe

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
