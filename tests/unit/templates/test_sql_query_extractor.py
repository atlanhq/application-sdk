"""Unit tests for SqlQueryExtractor template."""

from __future__ import annotations

import warnings

import pytest

from application_sdk.app.base import App
from application_sdk.app.task import is_task
from application_sdk.errors.leaves import UnimplementedError
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput,
    QueryBatchOutput,
    QueryExtractionInput,
    QueryExtractionOutput,
    QueryFetchInput,
)
from application_sdk.templates.sql_query_extractor import SqlQueryExtractor


class TestSqlQueryExtractorStructure:
    """Tests for SqlQueryExtractor class structure."""

    def test_is_app_subclass(self) -> None:
        assert issubclass(SqlQueryExtractor, App)

    def test_has_get_query_batches_task(self) -> None:
        method = SqlQueryExtractor.get_query_batches
        assert is_task(method)

    def test_has_fetch_queries_task(self) -> None:
        method = SqlQueryExtractor.fetch_queries
        assert is_task(method)

    def test_run_accepts_query_extraction_input(self) -> None:
        from typing import get_type_hints

        original_run = getattr(
            SqlQueryExtractor, "_original_run", SqlQueryExtractor.run
        )
        hints = get_type_hints(original_run)
        assert hints.get("input") is QueryExtractionInput

    def test_run_returns_query_extraction_output(self) -> None:
        from typing import get_type_hints

        original_run = getattr(
            SqlQueryExtractor, "_original_run", SqlQueryExtractor.run
        )
        hints = get_type_hints(original_run)
        assert hints.get("return") is QueryExtractionOutput

    def test_get_query_batches_input_type(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlQueryExtractor.get_query_batches)
        assert meta.input_type is QueryBatchInput

    def test_get_query_batches_output_type(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlQueryExtractor.get_query_batches)
        assert meta.output_type is QueryBatchOutput

    def test_fetch_queries_timeout(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlQueryExtractor.fetch_queries)
        assert meta.timeout_seconds == 3600

    def test_get_query_batches_timeout(self) -> None:
        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(SqlQueryExtractor.get_query_batches)
        assert meta.timeout_seconds == 600


class TestSqlQueryExtractorSubclass:
    """Tests for subclassing SqlQueryExtractor."""

    def test_sql_query_extractor_is_app_subclass(self) -> None:
        # Direct subclasses inherit _app_registered=True and don't auto-register.
        # Verify the base class itself is a proper App subclass.
        assert issubclass(SqlQueryExtractor, App)

    async def test_get_query_batches_raises_not_implemented_by_default(
        self,
    ) -> None:
        extractor = SqlQueryExtractor.__new__(SqlQueryExtractor)
        with pytest.raises(
            UnimplementedError, match="must implement get_query_batches"
        ):
            await extractor.get_query_batches(QueryBatchInput())

    async def test_fetch_queries_raises_not_implemented_by_default(self) -> None:
        extractor = SqlQueryExtractor.__new__(SqlQueryExtractor)
        with pytest.raises(UnimplementedError):
            await extractor.fetch_queries(QueryFetchInput())


class TestSubclassDeprecationNotice:
    def test_notice_names_app_with_own_query_tasks(
        self, clean_app_registry: object, clean_task_registry: object
    ) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)

            class _NoticeQueryExtractor(SqlQueryExtractor):
                pass

        messages = [
            str(w.message)
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "_NoticeQueryExtractor subclasses SqlQueryExtractor" in str(w.message)
        ]
        assert len(messages) == 1
        assert "application_sdk.app.App" in messages[0]
        assert "get_query_batches" in messages[0]
        assert "fetch_queries" in messages[0]
        assert "Use application_sdk.templates.SqlApp" not in messages[0]
        assert "v4.0.0" in messages[0]
