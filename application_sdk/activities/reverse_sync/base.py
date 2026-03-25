"""Base class for reverse sync activities — template method pattern."""

import json
import logging
from abc import abstractmethod
from typing import Any

from application_sdk.clients.sql import BaseSQLClient
from application_sdk.common.reverse_sync.event_parser import extract_source_tags
from application_sdk.common.reverse_sync.models import (
    ReverseSyncResult,
    SourceTagInfo,
    WriteBackSQL,
)

logger = logging.getLogger(__name__)


class BaseReverseSyncActivities:
    """Base class for source-specific reverse sync activities.

    Subclasses MUST override:
        - _build_tag_sqls(): Source-specific SQL generation
        - _get_sql_client(): Return the source's SQLClient instance
        - CONNECTOR_NAME: The connector identifier (e.g., "snowflake")
    """

    CONNECTOR_NAME: str = ""  # Override in subclass

    @staticmethod
    def _parse_source_tags(
        row: dict[str, Any], connector_filter: str | None = None
    ) -> list[SourceTagInfo]:
        """Parse mutatedDetails JSON from a Parquet row."""
        mutated_details = json.loads(row.get("mutated_details_json", "[]"))
        return extract_source_tags(mutated_details, connector_filter=connector_filter)

    @abstractmethod
    def _build_tag_sqls(
        self,
        operation_type: str,
        entity_type_name: str,
        entity_qualified_name: str,
        entity_name: str,
        table_qualified_name: str,
        view_qualified_name: str,
        tags: list[SourceTagInfo],
    ) -> list[WriteBackSQL]:
        """Build source-specific SQL statements. Override in each source app."""
        ...

    @abstractmethod
    async def _get_sql_client(self, connection_qualified_name: str) -> BaseSQLClient:
        """Return an initialized SQLClient for the given connection. Override in each source app."""
        ...

    async def process_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Process a single event row: parse -> build SQL -> execute.

        This is the template method — subclasses customize via
        _build_tag_sqls() and _get_sql_client().
        """
        operation_type = row["operation_type"]
        entity_type = row["entity_type_name"]
        entity_qn = row["entity_qualified_name"]
        entity_name = row.get("entity_name", "")
        connection_qn = row["connection_qualified_name"]
        table_qn = row.get("table_qualified_name", "")
        view_qn = row.get("view_qualified_name", "")

        tags = self._parse_source_tags(row, connector_filter=self.CONNECTOR_NAME)
        if not tags:
            return {"success": True, "skipped": True, "reason": "no_source_tags"}

        sqls = self._build_tag_sqls(
            operation_type=operation_type,
            entity_type_name=entity_type,
            entity_qualified_name=entity_qn,
            entity_name=entity_name,
            table_qualified_name=table_qn,
            view_qualified_name=view_qn,
            tags=tags,
        )
        if not sqls:
            return {"success": True, "skipped": True, "reason": "no_sql_generated"}

        sql_client = await self._get_sql_client(connection_qn)
        results: list[dict[str, Any]] = []
        for wb in sqls:
            try:
                logger.info("Executing: %s", wb.statement)
                await sql_client.get_results(wb.statement)
                results.append({"sql": wb.statement, "success": True})
            except Exception as e:
                logger.error("SQL failed: %s -> %s", wb.statement, e, exc_info=True)
                results.append({"sql": wb.statement, "success": False, "error": str(e)})

        all_ok = all(r["success"] for r in results)
        return {"success": all_ok, "sql_results": results}

    async def process_batch(self, rows: list[dict[str, Any]]) -> ReverseSyncResult:
        """Process a batch of event rows. Called by the Temporal activity."""
        result = ReverseSyncResult(total=len(rows))
        for row in rows:
            row_result = await self.process_row(row)
            if row_result.get("skipped"):
                result.skipped += 1
            elif row_result.get("success"):
                result.synced += 1
            else:
                result.failed += 1
                result.errors.append(
                    {
                        "entity": row.get("entity_qualified_name", ""),
                        "error": row_result.get("error", ""),
                    }
                )
        result.passed = result.synced + result.failed
        logger.info(
            "Batch: %d synced, %d skipped, %d failed / %d total",
            result.synced,
            result.skipped,
            result.failed,
            result.total,
        )
        return result
