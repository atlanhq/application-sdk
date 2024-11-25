import logging
from typing import Any, Dict, List

from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class ParallelizeQueryExecutor:
    def __init__(
        self,
        query: str,
        timestamp_column: str,
        chunk_size: int,
        current_marker: str,
        sql_ranged_replace_from: str,
        sql_ranged_replace_to: str,
        ranged_sql_start_key: str,
        ranged_sql_end_key: str,
        sql_resource: SQLResource,
    ):
        self.query = query
        self.timestamp_column = timestamp_column
        self.chunk_size = chunk_size
        self.current_marker = current_marker
        self.sql_ranged_replace_from = sql_ranged_replace_from
        self.sql_ranged_replace_to = sql_ranged_replace_to
        self.ranged_sql_start_key = ranged_sql_start_key
        self.ranged_sql_end_key = ranged_sql_end_key
        self.sql_resource = sql_resource

    async def execute(self) -> List[Dict[str, Any]]:
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be greater than 0")

        parallel_markers: List[Dict[str, Any]] = []

        try:
            await self._process_sql_chunk(self.query, 0, parallel_markers)
        except Exception as e:
            logger.error(f"Failed to process SQL: {e}")

        return parallel_markers

    async def _process_sql_chunk(
        self, sql: str, chunk_count: int, parallel_markers: List[Dict[str, Any]]
    ) -> tuple[int, int]:
        marked_sql = sql.replace(self.ranged_sql_start_key, self.current_marker)
        rewritten_query = f"WITH T AS ({marked_sql}) SELECT {self.timestamp_column} FROM T ORDER BY {self.timestamp_column} ASC"
        logger.info(f"Executing query: {rewritten_query}")

        chunk_start_marker = None
        chunk_end_marker = None
        record_count = 0
        last_marker = None

        async for result_batch in self.sql_resource.run_query(rewritten_query):
            for row in result_batch:
                timestamp = row[self.timestamp_column.lower()]
                new_marker = str(int(timestamp.timestamp() * 1000))

                if last_marker == new_marker:
                    logger.info("Skipping duplicate start time")
                    record_count += 1
                    continue

                if not chunk_start_marker:
                    chunk_start_marker = new_marker
                chunk_end_marker = new_marker
                record_count += 1
                last_marker = new_marker

                if record_count >= self.chunk_size:
                    self._get_chunked_sql(
                        sql,
                        chunk_count,
                        chunk_start_marker,
                        chunk_end_marker,
                        parallel_markers,
                        record_count,
                    )
                    chunk_count += 1
                    record_count = 0
                    chunk_start_marker = None
                    chunk_end_marker = None

        if record_count > 0:
            self._get_chunked_sql(
                sql,
                chunk_count,
                chunk_start_marker,
                chunk_end_marker,
                parallel_markers,
                record_count,
            )
            chunk_count += 1

        return chunk_count - 1, record_count

    def _get_chunked_sql(
        self,
        sql: str,
        chunk_count: int,
        start_marker: str | None,
        end_marker: str | None,
        parallel_markers: List[Dict[str, Any]],
        record_count: int,
    ) -> None:
        if not start_marker or not end_marker:
            return
        chunked_sql = sql.replace(
            self.sql_ranged_replace_from,
            self.sql_ranged_replace_to.replace(
                self.ranged_sql_start_key, start_marker
            ).replace(self.ranged_sql_end_key, end_marker),
        )

        logger.info(
            f"Processed {record_count} records in chunk {chunk_count}, "
            f"with start marker {start_marker} and end marker {end_marker}"
        )
        logger.info(f"Chunked SQL: {chunked_sql}")

        parallel_markers.append(
            {
                "sql": chunked_sql,
                "start": start_marker,
                "end": end_marker,
                "count": record_count,
            }
        )
