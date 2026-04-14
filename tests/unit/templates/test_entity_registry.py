"""Tests for ExtractableEntity and entity-driven orchestration in SqlMetadataExtractor."""

import pytest

from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    FetchDatabasesOutput,
)
from application_sdk.templates.entity import ExtractableEntity, default_result_key
from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor

# ---------------------------------------------------------------------------
# ExtractableEntity tests
# ---------------------------------------------------------------------------


class TestExtractableEntity:
    def test_defaults(self):
        e = ExtractableEntity(task_name="fetch_databases")
        assert e.phase == 1
        assert e.enabled is True
        assert e.timeout_seconds == 1800
        assert e.result_key == ""
        assert e.depends_on == ()

    def test_frozen(self):
        e = ExtractableEntity(task_name="fetch_databases")
        with pytest.raises(AttributeError):
            e.task_name = "fetch_schemas"  # type: ignore[misc]

    def test_custom_fields(self):
        e = ExtractableEntity(
            task_name="fetch_stages",
            phase=2,
            timeout_seconds=3600,
            result_key="stages_count",
        )
        assert e.task_name == "fetch_stages"
        assert e.phase == 2
        assert e.timeout_seconds == 3600
        assert e.result_key == "stages_count"

    def test_disabled_entity(self):
        e = ExtractableEntity(task_name="fetch_ai_models", enabled=False)
        assert e.enabled is False

    def test_result_key_default_derivation(self):
        """Empty result_key should default to '{base}_extracted' at runtime."""
        e = ExtractableEntity(task_name="fetch_stages")
        expected = "stages_extracted"
        actual = e.result_key or default_result_key(e.task_name)
        assert actual == expected

    def test_depends_on(self):
        e = ExtractableEntity(
            task_name="fetch_dynamic_tables",
            phase=2,
            depends_on=("fetch_tables",),
        )
        assert e.depends_on == ("fetch_tables",)


# ---------------------------------------------------------------------------
# default_result_key tests
# ---------------------------------------------------------------------------


class TestDefaultResultKey:
    def test_strips_fetch_prefix(self):
        assert default_result_key("fetch_databases") == "databases_extracted"

    def test_no_prefix(self):
        assert default_result_key("stages") == "stages_extracted"

    def test_private_method(self):
        assert (
            default_result_key("_fetch_queries_batched")
            == "_fetch_queries_batched_extracted"
        )


# ---------------------------------------------------------------------------
# _get_entities tests
# ---------------------------------------------------------------------------


class TestGetEntities:
    def test_empty_entities_falls_back_to_defaults(self):
        ext = SqlMetadataExtractor()
        assert ext.entities == []
        entities = ext._get_entities()
        assert len(entities) == 4
        task_names = [e.task_name for e in entities]
        assert task_names == [
            "fetch_databases",
            "fetch_schemas",
            "fetch_tables",
            "fetch_columns",
        ]

    def test_custom_entities_used(self):
        class Custom(SqlMetadataExtractor):
            entities = [
                ExtractableEntity(task_name="fetch_databases", phase=1),
                ExtractableEntity(task_name="fetch_stages", phase=2),
            ]

        ext = Custom()
        entities = ext._get_entities()
        assert len(entities) == 2
        assert entities[1].task_name == "fetch_stages"

    def test_disabled_entity_filtered(self):
        class WithDisabled(SqlMetadataExtractor):
            entities = [
                ExtractableEntity(task_name="fetch_databases", phase=1),
                ExtractableEntity(task_name="fetch_schemas", phase=1, enabled=False),
                ExtractableEntity(task_name="fetch_tables", phase=1),
            ]

        ext = WithDisabled()
        entities = ext._get_entities()
        assert len(entities) == 2
        assert all(e.task_name != "fetch_schemas" for e in entities)

    def test_all_disabled_returns_empty(self):
        class AllDisabled(SqlMetadataExtractor):
            entities = [
                ExtractableEntity(task_name="fetch_databases", enabled=False),
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
        entity = ExtractableEntity(task_name="fetch_databases")
        result_key, count = await ext._fetch_entity(entity, _make_input())
        assert result_key == "databases_extracted"
        assert count == 42

    @pytest.mark.asyncio
    async def test_custom_result_key(self):
        class Ext(SqlMetadataExtractor):
            async def fetch_databases(self, input):
                return FetchDatabasesOutput(total_record_count=7)

        ext = Ext()
        entity = ExtractableEntity(task_name="fetch_databases", result_key="db_count")
        result_key, count = await ext._fetch_entity(entity, _make_input())
        assert result_key == "db_count"
        assert count == 7

    @pytest.mark.asyncio
    async def test_missing_method_raises(self):
        ext = SqlMetadataExtractor()
        entity = ExtractableEntity(task_name="fetch_nonexistent_thing")
        with pytest.raises(NotImplementedError, match="no 'fetch_nonexistent_thing'"):
            await ext._fetch_entity(entity, _make_input())

    @pytest.mark.asyncio
    async def test_extra_entity_with_custom_method(self):
        """Custom entities dispatch to the method named by task_name."""

        class Ext(SqlMetadataExtractor):
            async def fetch_stages(self, input):
                return FetchDatabasesOutput(total_record_count=15)

        ext = Ext()
        entity = ExtractableEntity(task_name="fetch_stages", phase=2)
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
        entity = ExtractableEntity(task_name="fetch_databases")
        await ext._fetch_entity(entity, _make_input(credential_ref=ref))
        assert captured_input["cred_ref"].name == "my-cred"


# ---------------------------------------------------------------------------
# Phase grouping (unit test without Temporal)
# ---------------------------------------------------------------------------


class TestPhaseGrouping:
    def test_entities_grouped_by_phase(self):
        entities = [
            ExtractableEntity(task_name="fetch_databases", phase=1),
            ExtractableEntity(task_name="fetch_schemas", phase=1),
            ExtractableEntity(task_name="fetch_stages", phase=2),
            ExtractableEntity(task_name="fetch_streams", phase=2),
            ExtractableEntity(task_name="fetch_dynamic_tables", phase=3),
        ]

        phases: dict[int, list[ExtractableEntity]] = {}
        for e in entities:
            phases.setdefault(e.phase, []).append(e)

        assert sorted(phases.keys()) == [1, 2, 3]
        assert len(phases[1]) == 2
        assert len(phases[2]) == 2
        assert len(phases[3]) == 1
        assert phases[3][0].task_name == "fetch_dynamic_tables"


# ---------------------------------------------------------------------------
# run_entity_phases tests
# ---------------------------------------------------------------------------


class TestRunEntityPhases:
    @pytest.mark.asyncio
    async def test_collects_results_across_phases(self):
        class Ext(SqlMetadataExtractor):
            async def fetch_databases(self, input):
                return FetchDatabasesOutput(total_record_count=10)

            async def fetch_schemas(self, input):
                return FetchDatabasesOutput(total_record_count=20)

            async def fetch_stages(self, input):
                return FetchDatabasesOutput(total_record_count=5)

        ext = Ext()
        ext.__class__.entities = [
            ExtractableEntity(task_name="fetch_databases", phase=1),
            ExtractableEntity(task_name="fetch_schemas", phase=1),
            ExtractableEntity(task_name="fetch_stages", phase=2),
        ]

        from application_sdk.templates.entity import run_entity_phases

        results = await run_entity_phases(ext, ext._get_entities(), _make_input())
        assert results["databases_extracted"] == 10
        assert results["schemas_extracted"] == 20
        assert results["stages_extracted"] == 5

    @pytest.mark.asyncio
    async def test_error_in_phase_still_completes_siblings(self):
        """If one entity fails, siblings in the same phase still run."""
        call_log = []

        class Ext(SqlMetadataExtractor):
            async def fetch_databases(self, input):
                call_log.append("databases")
                raise ValueError("db error")

            async def fetch_schemas(self, input):
                call_log.append("schemas")
                return FetchDatabasesOutput(total_record_count=5)

        ext = Ext()
        ext.__class__.entities = [
            ExtractableEntity(task_name="fetch_databases", phase=1),
            ExtractableEntity(task_name="fetch_schemas", phase=1),
        ]

        from application_sdk.templates.entity import run_entity_phases

        with pytest.raises(ValueError, match="db error"):
            await run_entity_phases(ext, ext._get_entities(), _make_input())

        # Both methods should have been called (return_exceptions=True)
        assert "databases" in call_log
        assert "schemas" in call_log
