# from typing import Any, Dict

# import pytest

# from application_sdk.workflows.transformers.atlas import AtlasTransformer
# from application_sdk.workflows.transformers.const import (
#     COLUMN,
#     DATABASE,
#     SCHEMA,
#     TABLE,
#     VIEW,
# )


# @pytest.fixture
# def atlas_transformer() -> AtlasTransformer:
#     return AtlasTransformer(connector_name="postgres", connector_type="sql")


# def test_transform_database(
#     atlas_transformer: AtlasTransformer, sample_database_data: Dict[str, Any]
# ):
#     result = atlas_transformer.transform_metadata(
#         typename=DATABASE, data=sample_database_data
#     )

#     assert result is not None
#     assert result["type_name"] == DATABASE.capitalize()
#     assert result["attributes"]["name"] == "test_db"
#     assert result["attributes"]["schema_count"] == 5
#     assert result["attributes"]["qualified_name"] == "default/postgres/0/test_db"


# def test_transform_schema(
#     atlas_transformer: AtlasTransformer, sample_schema_data: Dict[str, Any]
# ):
#     result = atlas_transformer.transform_metadata(
#         typename=SCHEMA, data=sample_schema_data
#     )

#     assert result is not None
#     assert result["type_name"] == SCHEMA.capitalize()
#     assert result["attributes"]["name"] == "test_schema"
#     assert result["attributes"]["table_count"] == 10
#     assert result["attributes"]["views_count"] == 2
#     assert (
#         result["attributes"]["qualified_name"]
#         == "default/postgres/0/test_catalog/test_schema"
#     )


# def test_transform_table(
#     atlas_transformer: AtlasTransformer, sample_table_data: Dict[str, Any]
# ):
#     result = atlas_transformer.transform_metadata(
#         typename=TABLE, data=sample_table_data
#     )

#     assert result is not None
#     assert result["type_name"] == TABLE.capitalize()
#     assert result["attributes"]["name"] == "test_table"
#     assert result["attributes"]["column_count"] == 5
#     assert result["attributes"]["row_count"] == 1000
#     assert result["attributes"]["size_bytes"] == 1024


# def test_transform_view(
#     atlas_transformer: AtlasTransformer, sample_view_data: Dict[str, Any]
# ):
#     result = atlas_transformer.transform_metadata(typename=VIEW, data=sample_view_data)

#     assert result is not None
#     assert result["type_name"] == VIEW.capitalize()
#     assert result["attributes"]["name"] == "test_view"
#     assert result["attributes"]["column_count"] == 3
#     assert result["attributes"]["definition"] == "SELECT * FROM test_table"


# def test_transform_column(
#     atlas_transformer: AtlasTransformer, sample_column_data: Dict[str, Any]
# ):
#     result = atlas_transformer.transform_metadata(
#         typename=COLUMN, data=sample_column_data
#     )

#     assert result is not None
#     assert result["type_name"] == COLUMN.capitalize()
#     assert result["attributes"]["name"] == "test_column"
#     assert result["attributes"]["data_type"] == "varchar"
#     assert result["attributes"]["order"] == 1
#     assert result["attributes"]["is_nullable"] is True


# def test_invalid_type(
#     atlas_transformer: AtlasTransformer, sample_database_data: Dict[str, Any]
# ):
#     result = atlas_transformer.transform_metadata(
#         typename="INVALID_TYPE", data=sample_database_data
#     )
#     assert result is None


# # def test_missing_required_fields(atlas_transformer: AtlasTransformer):
# #     invalid_data = {"some_field": "some_value"}
# #     result = atlas_transformer.transform_metadata(typename=DATABASE, data=invalid_data)
# #     assert result is None

# # @pytest.mark.parametrize("field", ["database_name", "catalog_name", "schema_name", "table_name"])
# # def test_null_field_validation(atlas_transformer: AtlasTransformer, field: str, sample_database_data: Dict[str, Any]):
# #     sample_database_data[field] = None
# #     result = atlas_transformer.transform_metadata(typename=DATABASE, data=sample_database_data)
# #     assert result is None
