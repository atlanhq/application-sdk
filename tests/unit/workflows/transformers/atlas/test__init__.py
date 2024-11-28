import datetime
import json

import pytest
from pyatlan.model.assets import MaterialisedView, SnowflakeDynamicTable, Table, View

from application_sdk.workflows.transformers.atlas import AtlasTransformer


@pytest.fixture
def atlas_transformer():
    return AtlasTransformer(connector_name="snowflake")


@pytest.fixture
def base_qualified_name():
    return "default/snowflake/0"


def test_create_database_entity_basic(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "datname": "TEST_DB",
        "schema_count": 113,
        "remarks": "This is the description of the database",
        "database_owner": "owner",
        "database_id": "183",
        "created": "2024-06-04T15:13:07.000119+00:00",
        "last_altered": "2024-06-04T15:13:32.000647+00:00",
        "is_transient": "NO",
        "type": "STANDARD",
        "owner_role_type": "ROLE",
    }

    result = atlas_transformer._create_database_entity(data, base_qualified_name)  # type: ignore

    assert result is not None
    assert result.name == '"TEST_DB"'
    assert result.connection_qualified_name == base_qualified_name
    assert result.schema_count == 113
    assert result.description == json.dumps("This is the description of the database")
    assert result.source_created_by == "owner"
    assert (
        result.custom_attributes is not None
        and result.custom_attributes["source_id"] == "183"
    )
    assert result.source_created_at == datetime.datetime(
        2024, 6, 4, 15, 13, 7, 119, tzinfo=datetime.timezone.utc
    )
    assert result.source_updated_at == datetime.datetime(
        2024, 6, 4, 15, 13, 32, 647, tzinfo=datetime.timezone.utc
    )
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert result.last_sync_run_at is not None
    assert abs((result.last_sync_run_at - now).total_seconds()) < 5


def test_create_database_entity_with_long_description(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    description = "<p>" + "".join(["A"] * 200000) + "</p>"

    data = {
        "datname": "TEST_DB",
        "schema_count": 113,
        "comment": description,
        "database_owner": "owner",
        "database_id": "183",
        "created": "2024-06-04T15:13:07.000119+00:00",
        "last_altered": "2024-06-04T15:13:32.000647+00:00",
        "is_transient": "NO",
        "type": "STANDARD",
        "owner_role_type": "ROLE",
    }

    result = atlas_transformer._create_database_entity(data, base_qualified_name)  # type: ignore

    assert result is not None
    assert result.name == '"TEST_DB"'
    assert result.connection_qualified_name == base_qualified_name
    assert result.schema_count == 113
    assert result.description is not None and len(result.description) <= 100000
    assert result.source_created_by == "owner"
    assert (
        result.custom_attributes is not None
        and result.custom_attributes["source_id"] == "183"
    )
    assert result.source_created_at == datetime.datetime(
        2024,
        6,
        4,
        15,
        13,
        7,
        119,
        tzinfo=datetime.timezone.utc,
    )
    assert result.source_updated_at == datetime.datetime(
        2024,
        6,
        4,
        15,
        13,
        32,
        647,
        tzinfo=datetime.timezone.utc,
    )
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert result.last_sync_run_at is not None
    assert abs((result.last_sync_run_at - now).total_seconds()) < 5


def test_create_database_entity_without_datname(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "schema_count": 113,
        "remarks": "This is the description of the database",
        "database_owner": "owner",
        "database_id": "183",
        "created": "2024-06-04T15:13:07.000119+00:00",
        "last_altered": "2024-06-04T15:13:32.000647+00:00",
        "is_transient": "NO",
        "type": "STANDARD",
        "owner_role_type": "ROLE",
    }

    database_entity = atlas_transformer._create_database_entity(  # type: ignore
        data, base_qualified_name
    )
    assert database_entity is None


def test_create_database_entity_with_extra_info(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "datname": "test_db",
        "extra_info": [
            {
                "comment": "Extra info comment",
                "database_owner": "extra_user",
                "created": "2024-01-01T12:00:00.000+0000",
                "last_altered": "2024-01-02T12:00:00.000+0000",
            }
        ],
    }

    result = atlas_transformer._create_database_entity(data, base_qualified_name)  # type: ignore

    assert result is not None
    assert result.description == json.dumps("Extra info comment")
    assert result.source_created_by == "extra_user"
    assert result.source_created_at == datetime.datetime(
        2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc
    )
    assert result.source_updated_at == datetime.datetime(
        2024, 1, 2, 12, 0, tzinfo=datetime.timezone.utc
    )


def test_create_table_entities(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "table_name": "test_table",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "column_count": 10,
        "row_count": 1000,
        "table_owner": "test_owner",
        "view_definition": "CREATE MATERIALIZED VIEW mat_view AS SELECT * FROM test_table;",
        "is_secure": "NO",
        "remarks": None,
        "table_id": "123",
        "table_schema_id": "456",
        "table_schema": "test_schema",
        "table_catalog_id": "789",
        "table_catalog": "test_catalog",
        "is_transient": "NO",
        "is_iceberg": "NO",
        "is_dynamic": "NO",
        "clustering_key": None,
        "bytes": 1000,
        "retention_time": 1,
        "created": "2024-01-01T12:00:00.000+0000",
        "last_altered": "2024-01-02T12:00:00.000+0000",
        "last_ddl": "2024-01-02T12:00:00.000+0000",
        "last_ddl_by": "test_owner",
        "deleted": None,
        "auto_clustering_on": "NO",
        "comment": None,
        "owner_role_type": "ROLE",
        "instance_id": "abc123",
    }
    # Test view
    data["table_type"] = "VIEW"
    entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert isinstance(entity, View)
    assert entity is not None

    # Test materialized view
    data["table_type"] = "MATERIALIZED VIEW"
    entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert entity is not None
    assert isinstance(entity, MaterialisedView)

    # Test dynamic table
    data["table_type"] = "DYNAMIC TABLE"
    entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert entity is not None
    assert isinstance(entity, SnowflakeDynamicTable)
    assert entity.definition == json.dumps(
        "CREATE MATERIALIZED VIEW mat_view AS SELECT * FROM test_table;"
    )

    # Test regular table
    data["table_type"] = "BASE TABLE"
    entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert entity is not None
    assert entity.attributes.column_count == 10
    assert entity.attributes.row_count == 1000
    assert isinstance(entity, Table)

    assert entity.source_created_by == "test_owner"
    assert entity.source_created_at == datetime.datetime(
        2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc
    )
    assert entity.source_updated_at == datetime.datetime(
        2024, 1, 2, 12, 0, tzinfo=datetime.timezone.utc
    )
    assert (
        entity.custom_attributes is not None
        and entity.custom_attributes["is_transient"] == "NO"
    )
    assert entity.custom_attributes["last_ddl"] == "2024-01-02T12:00:00.000+0000"
    assert entity.custom_attributes["last_ddl_by"] == "test_owner"
    assert entity.custom_attributes["is_secure"] == "NO"
    assert entity.custom_attributes["retention_time"] == 1
    assert entity.custom_attributes["stage_url"] is None
    assert entity.custom_attributes["is_insertable_into"] is None
    assert entity.custom_attributes["number_columns_in_part_key"] is None
    assert entity.custom_attributes["columns_participating_in_part_key"] is None
    assert entity.custom_attributes["is_typed"] is None
    assert entity.custom_attributes["auto_clustering_on"] == "NO"
    assert entity.custom_attributes["engine"] is None
    assert entity.custom_attributes["auto_increment"] is None

    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert (
        entity.last_sync_run_at is not None
        and abs((entity.last_sync_run_at - now).total_seconds()) < 5
    )


def test_table_entity_with_long_description(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    description = "<p>" + "".join(["A"] * 200000) + "</p>"
    data = {
        "table_name": "test_table",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "column_count": 10,
        "row_count": 1000,
        "table_owner": "test_owner",
        "view_definition": [{"definition": "SELECT * FROM test_table"}],
        "remarks": description,
    }

    # Test regular table
    data["table_type"] = "BASE TABLE"
    entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert entity is not None
    assert entity.attributes.column_count == 10
    assert entity.attributes.row_count == 1000
    assert isinstance(entity, Table)
    assert entity.description is not None and len(entity.description) <= 100000


def test_table_entity_missing_required_fields(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "table_name": None,
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "table_type": "BASE TABLE",
        "table_owner": "test_owner",
    }

    # Test missing table name
    table_entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert table_entity is None

    # Test missing table catalog
    data["table_name"] = "test_table"
    data["table_cat"] = None
    table_entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert table_entity is None

    # Test missing table schema
    data["table_cat"] = "test_catalog"
    data["table_schem"] = None
    table_entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert table_entity is None

    # Test missing table type
    data["table_schem"] = "test_schema"
    data["table_type"] = None
    table_entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert table_entity is None

    # Test missing table owner
    data["table_type"] = "BASE TABLE"
    data["table_owner"] = None
    table_entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert table_entity is None

    # Data has all required fields
    data["table_owner"] = "test_owner"
    table_entity = atlas_transformer._create_table_entity(data, base_qualified_name)  # type: ignore
    assert table_entity is not None


def test_schema_entity_creation(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "schema_name": "test_schema",
        "catalog_name": "test_catalog",
        "table_count": 10,
        "view_count": 5,
        "remarks": "Test schema remarks",
        "created": "2024-01-01T12:00:00.000+0000",
        "last_altered": "2024-01-02T12:00:00.000+0000",
        "schema_owner": "test_owner",
        "schema_id": "123",
        "catalog_id": "456",
        "is_managed_access": "YES",
    }

    schema_entity = atlas_transformer._create_schema_entity(data, base_qualified_name)  # type: ignore
    assert schema_entity is not None
    assert schema_entity.name == '"test_schema"'
    assert schema_entity.database_name == "test_catalog"
    assert schema_entity.table_count == 10
    assert schema_entity.views_count == 5
    assert schema_entity.description == json.dumps("Test schema remarks")
    assert schema_entity.source_created_by == "test_owner"
    assert (
        schema_entity.custom_attributes is not None
        and schema_entity.custom_attributes["source_id"] == "123"
    )
    assert schema_entity.custom_attributes["catalog_id"] == "456"
    assert schema_entity.custom_attributes["is_managed_access"] == "YES"
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert (
        schema_entity.last_sync_run_at is not None
        and abs((schema_entity.last_sync_run_at - now).total_seconds()) < 5
    )


def test_schema_entity_creation_with_long_description(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    description = "<p>" + "".join(["A"] * 200000) + "</p>"
    data = {
        "schema_name": "test_schema",
        "catalog_name": "test_catalog",
        "table_count": 10,
        "view_count": 5,
        "remarks": description,
        "created": "2024-01-01T12:00:00.000+0000",
        "last_altered": "2024-01-02T12:00:00.000+0000",
        "schema_owner": "test_owner",
        "schema_id": "123",
        "catalog_id": "456",
        "is_managed_access": "YES",
    }

    schema_entity = atlas_transformer._create_schema_entity(data, base_qualified_name)  # type: ignore
    assert schema_entity is not None
    assert schema_entity.name == '"test_schema"'
    assert schema_entity.database_name == "test_catalog"
    assert schema_entity.table_count == 10
    assert schema_entity.views_count == 5
    assert (
        schema_entity.description is not None
        and len(schema_entity.description) <= 100000
    )
    assert schema_entity.source_created_by == "test_owner"
    assert (
        schema_entity.custom_attributes is not None
        and schema_entity.custom_attributes["source_id"] == "123"
    )
    assert schema_entity.custom_attributes["catalog_id"] == "456"
    assert schema_entity.custom_attributes["is_managed_access"] == "YES"
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert (
        schema_entity.last_sync_run_at is not None
        and abs((schema_entity.last_sync_run_at - now).total_seconds()) < 5
    )


def test_schema_entity_missing_required_fields(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "schema_name": None,
        "catalog_name": "test_catalog",
    }

    # Test missing schema name
    schema_entity = atlas_transformer._create_schema_entity(data, base_qualified_name)  # type: ignore
    assert schema_entity is None

    # Test missing catalog name
    data["schema_name"] = "test_schema"
    data["catalog_name"] = None
    schema_entity = atlas_transformer._create_schema_entity(data, base_qualified_name)  # type: ignore
    assert schema_entity is None

    # Data has all required fields
    data["catalog_name"] = "test_catalog"
    schema_entity = atlas_transformer._create_schema_entity(data, base_qualified_name)  # type: ignore
    assert schema_entity is not None


def test_column_entity_creation(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "column_name": "test_column",
        "table_type": "TABLE",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "table_name": "test_table",
        "data_type": "VARCHAR",
        "ordinal_position": 1,
        "remarks": "Test column remarks",
        "is_nullable": "YES",
        "is_partition": "YES",
        "partition_order": 1,
        "primary_key": "YES",
        "foreign_key": "YES",
        "character_maximum_length": 255,
        "numeric_precision": 10,
        "numeric_scale": 2,
        "is_self_referencing": "YES",
        "column_id": "789",
        "table_catalog_id": "456",
        "table_schema_id": "123",
        "table_id": "321",
        "character_octet_length": 255,
        "is_autoincrement": "YES",
        "is_generatedcolumn": "NO",
        "extra_info": "test info",
        "buffer_length": 1024,
        "column_size": 255,
    }

    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.name == '"test_column"'
    assert column_entity.database_name == "test_catalog"
    assert column_entity.schema_name == "test_schema"
    assert column_entity.description == json.dumps("Test column remarks")
    assert column_entity.is_nullable is True
    assert column_entity.is_partition is True
    assert column_entity.partition_order == 1
    assert column_entity.is_primary is True
    assert column_entity.is_foreign is True
    assert column_entity.max_length == 255
    assert column_entity.precision == 10
    assert column_entity.numeric_scale == 2
    assert column_entity.attributes.table_name == '"test_table"'
    assert (
        column_entity.attributes.table_qualified_name
        == f'"{base_qualified_name}/test_catalog/test_schema/test_table"'
    )
    assert (
        column_entity.custom_attributes is not None
        and column_entity.custom_attributes["is_self_referencing"] == "YES"
    )
    assert column_entity.custom_attributes["source_id"] == "789"
    assert column_entity.custom_attributes["catalog_id"] == "456"
    assert column_entity.custom_attributes["schema_id"] == "123"
    assert column_entity.custom_attributes["table_id"] == "321"
    assert column_entity.custom_attributes["character_octet_length"] == 255
    assert column_entity.custom_attributes["is_auto_increment"] == "YES"
    assert column_entity.custom_attributes["is_generated"] == "NO"
    assert column_entity.custom_attributes["extra_info"] == "test info"
    assert column_entity.custom_attributes["buffer_length"] == 1024
    assert column_entity.custom_attributes["column_size"] == 255
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert (
        column_entity.last_sync_run_at is not None
        and abs((column_entity.last_sync_run_at - now).total_seconds()) < 5
    )


def test_column_entity_missing_required_fields(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "column_name": None,
        "table_type": "TABLE",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "table_name": "test_table",
        "data_type": "VARCHAR",
        "ordinal_position": 1,
    }

    # Test missing column name
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Test missing table type
    data["column_name"] = "test_column"
    data["table_type"] = None
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Test missing table catalog
    data["table_type"] = "TABLE"
    data["table_cat"] = None
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Test missing table schema
    data["table_cat"] = "test_catalog"
    data["table_schem"] = None
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Test missing table name
    data["table_schem"] = "test_schema"
    data["table_name"] = None
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Test missing data type
    data["table_name"] = "test_table"
    data["data_type"] = None
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Test missing column order
    data["data_type"] = "VARCHAR"
    data["ordinal_position"] = None
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is None

    # Data has all required fields
    data["ordinal_position"] = 1
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None


def test_column_entity_with_long_description(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    description = "<p>" + "".join(["A"] * 200000) + "</p>"
    data = {
        "column_name": "test_column",
        "table_type": "TABLE",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "table_name": "test_table",
        "data_type": "VARCHAR",
        "remarks": description,
        "ordinal_position": 1,
    }

    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert (
        column_entity.description is not None
        and len(column_entity.description) <= 100000
    )


def test_column_entity_order(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "column_name": "test_column",
        "table_type": "TABLE",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "table_name": "test_table",
        "ordinal_position": 1,
        "data_type": "VARCHAR",
    }
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.order == 1

    del data["ordinal_position"]
    data["column_id"] = "10"
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.order == 10

    del data["column_id"]
    data["internal_column_id"] = "789"
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.order == 789


def test_column_entity_view_types(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    base_data = {
        "column_name": "test_column",
        "table_cat": "test_catalog",
        "table_schem": "test_schema",
        "table_name": "test_table",
        "data_type": "VARCHAR",
        "ordinal_position": 1,
    }

    # Test MATERIALIZED VIEW
    data = base_data.copy()
    data["table_type"] = "MATERIALIZED VIEW"
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.attributes.view_name == '"test_table"'
    assert hasattr(column_entity.attributes, "materialised_view")

    # Test VIEW
    data = base_data.copy()
    data["table_type"] = "VIEW"
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.attributes.view_name == '"test_table"'
    assert hasattr(column_entity.attributes, "view")

    # Test regular TABLE
    data = base_data.copy()
    data["table_type"] = "TABLE"
    column_entity = atlas_transformer._create_column_entity(data, base_qualified_name)  # type: ignore
    assert column_entity is not None
    assert column_entity.attributes.table_name == '"test_table"'
    assert hasattr(column_entity.attributes, "table")
