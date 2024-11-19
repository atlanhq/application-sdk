# from typing import Any, Dict
# import pytest
# from application_sdk.workflows.transformers.phoenix import PhoenixTransformer
# from application_sdk.workflows.transformers.const import DATABASE, SCHEMA, TABLE, VIEW, COLUMN

# @pytest.fixture
# def phoenix_transformer() -> PhoenixTransformer:
#     return PhoenixTransformer(connector_name="test_connector")

# def test_transform_database(phoenix_transformer: PhoenixTransformer, sample_database_data: Dict[str, Any]):
#     result = phoenix_transformer.transform_metadata(DATABASE, sample_database_data)

#     assert result is not None
#     assert result["name"] == "test_db"
#     assert result["typeName"] == DATABASE
#     assert result["URI"] == "/test_connector/phoenix/test_db"
#     assert result["namespace"]["name"] == "test_connector-phoenix"

# def test_transform_schema(phoenix_transformer: PhoenixTransformer, sample_schema_data: Dict[str, Any]):
#     result = phoenix_transformer.transform_metadata(SCHEMA, sample_schema_data)

#     assert result is not None
#     assert result["name"] == "test_schema"
#     assert result["typeName"] == SCHEMA
#     assert result["URI"] == "/test_connector/phoenix/test_catalog/test_schema"

# def test_transform_table(phoenix_transformer: PhoenixTransformer, sample_table_data: Dict[str, Any]):
#     result = phoenix_transformer.transform_metadata(TABLE, sample_table_data)

#     assert result is not None
#     assert result["name"] == "test_table"
#     assert result["typeName"] == TABLE
#     assert result["isPartition"] is False
#     assert result["isSearchable"] is True
#     assert result["URI"] == "/test_connector/phoenix/test_catalog/test_schema/test_table"

# def test_transform_view(phoenix_transformer: PhoenixTransformer, sample_view_data: Dict[str, Any]):
#     result = phoenix_transformer.transform_metadata(VIEW, sample_view_data)

#     assert result is not None
#     assert result["name"] == "test_view"
#     assert result["typeName"] == VIEW
#     assert result["isSearchable"] is True
#     assert result["URI"] == "/test_connector/phoenix/test_catalog/test_schema/test_view"

# def test_transform_column(phoenix_transformer: PhoenixTransformer, sample_column_data: Dict[str, Any]):
#     result = phoenix_transformer.transform_metadata(COLUMN, sample_column_data)

#     assert result is not None
#     assert result["name"] == "test_column"
#     assert result["typeName"] == COLUMN
#     assert result["order"] == 1
#     assert result["dataType"] == "varchar"
#     assert result["constraints"]["notNull"] is True
#     assert result["isSearchable"] is True

# def test_invalid_type(phoenix_transformer: PhoenixTransformer, sample_database_data: Dict[str, Any]):
#     result = phoenix_transformer.transform_metadata(typename="INVALID_TYPE", data=sample_database_data)
#     assert result is not None  # Phoenix creates a default entity for unknown types
#     assert result["typeName"] == "INVALID_TYPE"

# def test_missing_required_fields(phoenix_transformer: PhoenixTransformer):
#     invalid_data = {"some_field": "some_value"}
#     result = phoenix_transformer.transform_metadata(typename=DATABASE, data=invalid_data)
#     assert result is None

# @pytest.mark.parametrize("field", [
#     "database_name",
#     "schema_name",
#     "table_name",
#     "column_name"
# ])
# def test_null_field_validation(phoenix_transformer: PhoenixTransformer, field: str, sample_database_data: Dict[str, Any]):
#     sample_database_data[field] = None
#     result = phoenix_transformer.transform_metadata(typename=DATABASE, data=sample_database_data)
#     assert result is None

# def test_uri_building(phoenix_transformer: PhoenixTransformer):
#     uri = phoenix_transformer._build_uri("part1", "part2", "part3")
#     assert uri == "/test_connector/phoenix/part1/part2/part3"
