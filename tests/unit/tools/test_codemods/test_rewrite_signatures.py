"""Tests for A3: RewriteSignaturesCodemod."""

from __future__ import annotations

import libcst as cst

from tests.unit.tools.test_codemods.conftest import transform
from tools.migrate_v3.codemods.rewrite_signatures import RewriteSignaturesCodemod


class TestSqlMetadataMethods:
    def test_fetch_databases(self) -> None:
        source = """
            class MyConnector:
                async def fetch_databases(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: FetchDatabasesInput" in code
        assert "FetchDatabasesOutput" in code
        assert "workflow_args" not in code

    def test_fetch_schemas(self) -> None:
        source = """
            class MyConnector:
                async def fetch_schemas(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: FetchSchemasInput" in code
        assert "FetchSchemasOutput" in code

    def test_fetch_tables(self) -> None:
        source = """
            class MyConnector:
                async def fetch_tables(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: FetchTablesInput" in code
        assert "FetchTablesOutput" in code

    def test_fetch_columns(self) -> None:
        source = """
            class MyConnector:
                async def fetch_columns(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: FetchColumnsInput" in code
        assert "FetchColumnsOutput" in code

    def test_fetch_procedures(self) -> None:
        source = """
            class MyConnector:
                async def fetch_procedures(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: FetchProceduresInput" in code
        assert "FetchProceduresOutput" in code

    def test_transform_data(self) -> None:
        source = """
            class MyConnector:
                async def transform_data(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: TransformInput" in code
        assert "TransformOutput" in code

    def test_run_sql_metadata(self) -> None:
        source = """
            class MyConnector:
                async def run(self, workflow_config):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert "input: ExtractionInput" in code
        assert "ExtractionOutput" in code


class TestSqlQueryMethods:
    def test_run_sql_query(self) -> None:
        source = """
            class MyConnector:
                async def run(self, workflow_config):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_query"
        )
        assert "input: QueryExtractionInput" in code
        assert "QueryExtractionOutput" in code

    def test_get_query_batches(self) -> None:
        source = """
            class MyConnector:
                async def get_query_batches(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_query"
        )
        assert "input: QueryBatchInput" in code
        assert "QueryBatchOutput" in code

    def test_fetch_queries(self) -> None:
        source = """
            class MyConnector:
                async def fetch_queries(self, workflow_args):
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_query"
        )
        assert "input: QueryFetchInput" in code
        assert "QueryFetchOutput" in code


class TestSkipConditions:
    def test_already_migrated_param_skipped(self) -> None:
        source = """
            class MyConnector:
                async def fetch_databases(self, input: FetchDatabasesInput) -> FetchDatabasesOutput:
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert not changes
        assert code.count("FetchDatabasesInput") == 1  # unchanged

    def test_unknown_method_skipped(self) -> None:
        source = """
            class MyConnector:
                async def do_something_custom(self, workflow_args):
                    pass
        """
        code, changes = transform(source, RewriteSignaturesCodemod)
        assert not changes
        assert "workflow_args" in code

    def test_method_without_old_param_skipped(self) -> None:
        source = """
            class MyConnector:
                async def fetch_databases(self, conn_string: str) -> None:
                    pass
        """
        code, changes = transform(
            source, RewriteSignaturesCodemod, connector_type="sql_metadata"
        )
        assert not changes


class TestImportsToAdd:
    def test_imports_collected(self) -> None:
        source = """
            class MyConnector:
                async def fetch_schemas(self, workflow_args):
                    pass
        """
        tree = cst.parse_module(source.strip() + "\n")
        codemod = RewriteSignaturesCodemod(connector_type="sql_metadata")
        codemod.transform(tree)
        modules = [m for m, _ in codemod.imports_to_add]
        assert all(
            m == "application_sdk.templates.contracts.sql_metadata" for m in modules
        )
        names = [n for _, n in codemod.imports_to_add]
        assert "FetchSchemasInput" in names
        assert "FetchSchemasOutput" in names
