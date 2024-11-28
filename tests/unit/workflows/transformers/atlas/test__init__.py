import datetime
import json

import pytest

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
        "created": "2024-06-04T15:13:07.000119-07:00",
        "last_altered": "2024-06-04T15:13:32.000647-07:00",
        "is_transient": "NO",
        "type": "STANDARD",
        "owner_role_type": "ROLE",
    }

    result = atlas_transformer._create_database_entity(data, base_qualified_name)

    assert result is not None
    assert result.name == '"TEST_DB"'
    assert result.connection_qualified_name == base_qualified_name
    assert result.schema_count == 113
    assert result.description == json.dumps("This is the description of the database")
    assert result.source_created_by == "owner"
    assert result.custom_attributes["source_id"] == "183"
    assert result.source_created_at == datetime.datetime(
        2024,
        6,
        4,
        15,
        13,
        7,
        119,
        tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=61200)),
    )
    assert result.source_updated_at == datetime.datetime(
        2024,
        6,
        4,
        15,
        13,
        32,
        647,
        tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=61200)),
    )
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
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
        "created": "2024-06-04T15:13:07.000119-07:00",
        "last_altered": "2024-06-04T15:13:32.000647-07:00",
        "is_transient": "NO",
        "type": "STANDARD",
        "owner_role_type": "ROLE",
    }

    result = atlas_transformer._create_database_entity(data, base_qualified_name)

    assert result is not None
    assert result.name == '"TEST_DB"'
    assert result.connection_qualified_name == base_qualified_name
    assert result.schema_count == 113
    assert len(result.description) <= 100000
    assert result.source_created_by == "owner"
    assert result.custom_attributes["source_id"] == "183"
    assert result.source_created_at == datetime.datetime(
        2024,
        6,
        4,
        15,
        13,
        7,
        119,
        tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=61200)),
    )
    assert result.source_updated_at == datetime.datetime(
        2024,
        6,
        4,
        15,
        13,
        32,
        647,
        tzinfo=datetime.timezone(datetime.timedelta(days=-1, seconds=61200)),
    )
    # Verify last_sync_run_at is within 5 seconds of now
    now = datetime.datetime.now()
    assert abs((result.last_sync_run_at - now).total_seconds()) < 5


def test_create_database_entity_without_datname(
    atlas_transformer: AtlasTransformer, base_qualified_name: str
):
    data = {
        "schema_count": 113,
        "remarks": "This is the description of the database",
        "database_owner": "owner",
        "database_id": "183",
        "created": "2024-06-04T15:13:07.000119-07:00",
        "last_altered": "2024-06-04T15:13:32.000647-07:00",
        "is_transient": "NO",
        "type": "STANDARD",
        "owner_role_type": "ROLE",
    }

    with pytest.raises(AssertionError) as exc_info:
        atlas_transformer._create_database_entity(data, base_qualified_name)

    assert str(exc_info.value) == "Database name cannot be None"


def test_create_database_entity_with_extra_info(atlas_transformer, base_qualified_name):
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

    result = atlas_transformer._create_database_entity(data, base_qualified_name)

    assert result is not None
    assert result.description == "Extra info comment"
    assert result.source_created_by == "extra_user"
    assert result.source_created_at == datetime(2024, 1, 1, 12, 0)
    assert result.source_updated_at == datetime(2024, 1, 2, 12, 0)


def test_create_database_entity_missing_name(atlas_transformer, base_qualified_name):
    data = {
        "datname": None,
    }

    result = atlas_transformer._create_database_entity(data, base_qualified_name)
    assert result is None


def test_create_table_entities(atlas_transformer, base_qualified_name):
    data = {
        "table_name": "test_table",
        "table_catalog": "test_catalog",
        "table_schema": "test_schema",
        "column_count": 10,
        "row_count": 1000,
    }

    # Test regular table
    data["table_type"] = "BASE TABLE"
    entity = atlas_transformer._create_table_entity(data, base_qualified_name)
    assert entity is not None
    assert entity.attributes.column_count == 10
    assert entity.attributes.row_count == 1000

    # Test view
    data["table_type"] = "VIEW"
    entity = atlas_transformer._create_view_entity(data, base_qualified_name)
    assert entity is not None

    # Test materialized view
    data["table_type"] = "MATERIALIZED VIEW"
    entity = atlas_transformer._create_materialized_view_entity(
        data, base_qualified_name
    )
    assert entity is not None

    # Test dynamic table
    data["table_type"] = "DYNAMIC TABLE"
    entity = atlas_transformer._create_dynamic_table_entity(data, base_qualified_name)
    assert entity is not None


def test_table_entity_missing_required_fields(atlas_transformer, base_qualified_name):
    data = {
        "table_name": None,
        "table_catalog": "test_catalog",
        "table_schema": "test_schema",
    }

    with pytest.raises(AssertionError, match="Table name cannot be None"):
        atlas_transformer._create_table_entity(data, base_qualified_name)

    data["table_name"] = "test_table"
    data["table_catalog"] = None

    with pytest.raises(AssertionError, match="Table catalog cannot be None"):
        atlas_transformer._create_table_entity(data, base_qualified_name)
