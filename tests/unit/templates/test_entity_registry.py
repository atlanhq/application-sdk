"""Tests for EntityDef and entity-driven orchestration in SqlMetadataExtractor."""

import pytest

from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    FetchDatabasesOutput,
)
from application_sdk.templates.entity import EntityDef
from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor

# ---------------------------------------------------------------------------
# EntityDef tests
# ---------------------------------------------------------------------------


class TestEntityDef:
    def test_defaults(self):
        e = EntityDef(name="databases")
        assert e.phase == 1
        assert e.enabled is True
        assert e.timeout_seconds == 1800
        assert e.sql == ""
        assert e.endpoint == ""
        assert e.result_key == ""
        assert e.depends_on == ()

    def test_frozen(self):
        e = EntityDef(name="databases")
        with pytest.raises(AttributeError):
            e.name = "schemas"  # type: ignore[misc]

    def test_custom_fields(self):
        e = EntityDef(
            name="stages",
            sql="SELECT * FROM STAGES",
            phase=2,
            timeout_seconds=3600,
            result_key="stages_count",
        )
        assert e.name == "stages"
        assert e.sql == "SELECT * FROM STAGES"
        assert e.phase == 2
        assert e.timeout_seconds == 3600
        assert e.result_key == "stages_count"

    def test_disabled_entity(self):
        e = EntityDef(name="ai_models", enabled=False)
        assert e.enabled is False

    def test_api_entity(self):
        e = EntityDef(
            name="workbooks",
            endpoint="/api/3.1/workbooks",
            pagination="offset",
            response_items_key="workbooks.workbook",
        )
        assert e.endpoint == "/api/3.1/workbooks"
        assert e.pagination == "offset"
        assert e.response_items_key == "workbooks.workbook"

    def test_result_key_default_derivation(self):
        """Empty result_key should default to '{name}_extracted' at runtime."""
        e = EntityDef(name="stages")
        expected = f"{e.name}_extracted"
        actual = e.result_key or f"{e.name}_extracted"
        assert actual == expected

    def test_depends_on(self):
        e = EntityDef(name="dynamic_tables", phase=2, depends_on=("tables",))
        assert e.depends_on == ("tables",)


# ---------------------------------------------------------------------------
# _get_entities tests
# ---------------------------------------------------------------------------


class TestGetEntities:
    def test_empty_entities_falls_back_to_defaults(self):
        ext = SqlMetadataExtractor()
        assert ext.entities == []
        entities = ext._get_entities()
        assert len(entities) == 4
        names = [e.name for e in entities]
        assert names == ["databases", "schemas", "tables", "columns"]

    def test_custom_entities_used(self):
        class Custom(SqlMetadataExtractor):
            entities = [
                EntityDef(name="databases", phase=1),
                EntityDef(name="stages", phase=2),
            ]

        ext = Custom()
        entities = ext._get_entities()
        assert len(entities) == 2
        assert entities[1].name == "stages"

    def test_disabled_entity_filtered(self):
        class WithDisabled(SqlMetadataExtractor):
            entities = [
                EntityDef(name="databases", phase=1),
                EntityDef(name="schemas", phase=1, enabled=False),
                EntityDef(name="tables", phase=1),
            ]

        ext = WithDisabled()
        entities = ext._get_entities()
        assert len(entities) == 2
        assert all(e.name != "schemas" for e in entities)

    def test_all_disabled_returns_empty(self):
        class AllDisabled(SqlMetadataExtractor):
            entities = [
                EntityDef(name="databases", enabled=False),
            ]

        ext = AllDisabled()
        assert ext._get_entities() == []


# ---------------------------------------------------------------------------
# _fetch_entity tests
# ---------------------------------------------------------------------------


def _make_input(**overrides):
    defaults = {
        "workflow_id": "test-wf",
        "connection": {
            "connection_name": "test",
            "connection_qualified_name": "default/pg/123",
        },
    }
    defaults.update(overrides)
    return ExtractionInput(**defaults)


class TestFetchEntity:
    @pytest.mark.asyncio
    async def test_dispatches_to_named_method(self):
        class Ext(SqlMetadataExtractor):
            async def fetch_databases(self, input):
                return FetchDatabasesOutput(total_record_count=42)

        ext = Ext()
        entity = EntityDef(name="databases")
        result_key, count = await ext._fetch_entity(entity, _make_input())
        assert result_key == "databases_extracted"
        assert count == 42

    @pytest.mark.asyncio
    async def test_custom_result_key(self):
        class Ext(SqlMetadataExtractor):
            async def fetch_databases(self, input):
                return FetchDatabasesOutput(total_record_count=7)

        ext = Ext()
        entity = EntityDef(name="databases", result_key="db_count")
        result_key, count = await ext._fetch_entity(entity, _make_input())
        assert result_key == "db_count"
        assert count == 7

    @pytest.mark.asyncio
    async def test_missing_method_raises(self):
        ext = SqlMetadataExtractor()
        entity = EntityDef(name="nonexistent_thing")
        with pytest.raises(NotImplementedError, match="no fetch_nonexistent_thing"):
            await ext._fetch_entity(entity, _make_input())

    @pytest.mark.asyncio
    async def test_extra_entity_with_custom_method(self):
        """Custom entities dispatch to custom fetch methods."""

        class Ext(SqlMetadataExtractor):
            async def fetch_stages(self, input):
                return FetchDatabasesOutput(total_record_count=15)

        ext = Ext()
        entity = EntityDef(name="stages", phase=2)
        result_key, count = await ext._fetch_entity(entity, _make_input())
        assert result_key == "stages_extracted"
        assert count == 15

    @pytest.mark.asyncio
    async def test_credential_ref_passed_to_task_input(self):
        """Verify credential_ref from ExtractionInput is forwarded."""
        from application_sdk.credentials.ref import CredentialRef

        captured_input = {}

        class Ext(SqlMetadataExtractor):
            async def fetch_databases(self, input):
                captured_input["cred_ref"] = input.credential_ref
                return FetchDatabasesOutput(total_record_count=1)

        ext = Ext()
        ref = CredentialRef(name="my-cred", credential_type="basic")
        entity = EntityDef(name="databases")
        await ext._fetch_entity(entity, _make_input(credential_ref=ref))
        assert captured_input["cred_ref"].name == "my-cred"


# ---------------------------------------------------------------------------
# Phase grouping (unit test without Temporal)
# ---------------------------------------------------------------------------


class TestPhaseGrouping:
    def test_entities_grouped_by_phase(self):
        entities = [
            EntityDef(name="databases", phase=1),
            EntityDef(name="schemas", phase=1),
            EntityDef(name="stages", phase=2),
            EntityDef(name="streams", phase=2),
            EntityDef(name="dynamic_tables", phase=3),
        ]

        phases: dict[int, list[EntityDef]] = {}
        for e in entities:
            phases.setdefault(e.phase, []).append(e)

        assert sorted(phases.keys()) == [1, 2, 3]
        assert len(phases[1]) == 2
        assert len(phases[2]) == 2
        assert len(phases[3]) == 1
        assert phases[3][0].name == "dynamic_tables"
