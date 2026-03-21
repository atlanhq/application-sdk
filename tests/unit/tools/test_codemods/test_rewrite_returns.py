"""Tests for A4: RewriteReturnsCodemod."""

from __future__ import annotations

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.rewrite_returns import RewriteReturnsCodemod


class TestReturnCallRewrite:
    def test_activity_statistics_rewritten(self) -> None:
        source = """
            class MyConnector:
                async def fetch_databases(self, input):
                    return ActivityStatistics(total_record_count=5)
        """
        code, changes = transform(
            source, RewriteReturnsCodemod, connector_type="sql_metadata"
        )
        assert "ActivityStatistics" not in code
        assert "FetchDatabasesOutput" in code
        assert any("FetchDatabasesOutput" in c for c in changes)

    def test_task_statistics_rewritten(self) -> None:
        source = """
            class MyConnector:
                async def fetch_schemas(self, input):
                    return TaskStatistics(total_record_count=3)
        """
        code, changes = transform(
            source, RewriteReturnsCodemod, connector_type="sql_metadata"
        )
        assert "TaskStatistics" not in code
        assert "FetchSchemasOutput" in code

    def test_multiple_returns_in_one_method(self) -> None:
        source = """
            class MyConnector:
                async def fetch_tables(self, input):
                    if something:
                        return ActivityStatistics(total_record_count=0)
                    return ActivityStatistics(total_record_count=10)
        """
        code, changes = transform(
            source, RewriteReturnsCodemod, connector_type="sql_metadata"
        )
        assert "ActivityStatistics" not in code
        assert code.count("FetchTablesOutput") == 2

    def test_unknown_method_not_rewritten(self) -> None:
        source = """
            class MyConnector:
                async def custom_method(self, input):
                    return ActivityStatistics(total_record_count=1)
        """
        code, changes = transform(source, RewriteReturnsCodemod)
        # When method not in contract mapping, no rewrite
        assert not changes or all("custom_method" not in c for c in changes)

    def test_nested_function_not_in_mapping_stays(self) -> None:
        """Inner function named 'inner' is not in mapping; its return stays."""
        source = """
            class MyConnector:
                async def fetch_columns(self, input):
                    async def inner():
                        return ActivityStatistics(total_record_count=0)
                    return ActivityStatistics(total_record_count=5)
        """
        code, changes = transform(
            source, RewriteReturnsCodemod, connector_type="sql_metadata"
        )
        # The outer fetch_columns return is rewritten; inner() is not in mapping
        assert "FetchColumnsOutput" in code
        # The inner() function's ActivityStatistics is left (inner not in mapping)
        assert code.count("ActivityStatistics") == 1


class TestAnnotationRewrite:
    def test_simple_annotation_rewritten(self) -> None:
        source = """
            class MyConnector:
                async def fetch_databases(self, input):
                    result: ActivityStatistics = ActivityStatistics()
                    return result
        """
        code, changes = transform(
            source, RewriteReturnsCodemod, connector_type="sql_metadata"
        )
        # The annotation should be rewritten
        assert "ActivityStatistics" not in code or code.count("ActivityStatistics") == 0

    def test_optional_annotation_rewritten(self) -> None:
        source = """
            class MyConnector:
                async def fetch_schemas(self, input):
                    result: Optional[ActivityStatistics] = None
                    return result
        """
        code, changes = transform(
            source, RewriteReturnsCodemod, connector_type="sql_metadata"
        )
        assert "ActivityStatistics" not in code
        assert "FetchSchemasOutput" in code


class TestImportsToAdd:
    def test_output_type_import_collected(self) -> None:
        import libcst as cst

        from tools.migrate_v3.codemods.rewrite_returns import RewriteReturnsCodemod as C

        source = (
            """
            class MyConnector:
                async def fetch_procedures(self, input):
                    return ActivityStatistics(total_record_count=0)
        """.strip()
            + "\n"
        )
        tree = cst.parse_module(source)
        codemod = C(connector_type="sql_metadata")
        codemod.transform(tree)
        names = [n for _, n in codemod.imports_to_add]
        assert "FetchProceduresOutput" in names
