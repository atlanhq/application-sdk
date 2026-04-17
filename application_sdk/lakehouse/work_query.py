"""Work query runner for finding unprocessed lakehouse events."""

from __future__ import annotations

import logging
from typing import Any

import duckdb
import pyarrow as pa

logger = logging.getLogger(__name__)

_WORK_QUERY = """
WITH ranked AS (
    SELECT *,
        ROW_NUMBER() OVER (
            PARTITION BY asset_id, kind ORDER BY ingested_at DESC
        ) as rn
    FROM events
),
latest_event AS (
    SELECT * FROM ranked WHERE kind = 'EVENT' AND rn = 1
),
latest_outcome AS (
    SELECT asset_id, status, event_id as outcome_event_id, retry_count
    FROM ranked WHERE kind = 'OUTCOME' AND rn = 1
)
SELECT e.*, COALESCE(o.retry_count, 0) as current_retry_count
FROM latest_event e
LEFT JOIN latest_outcome o ON e.asset_id = o.asset_id
WHERE (o.asset_id IS NULL
   OR o.status = 'RETRY'
   OR o.outcome_event_id != e.event_id)
  AND COALESCE(o.retry_count, 0) < {max_retries}
ORDER BY e.ingested_at ASC
LIMIT {limit}
"""


class WorkQueryRunner:
    def __init__(self, max_retries: int = 5) -> None:
        self._max_retries = max_retries

    def run(self, arrow_table: pa.Table, limit: int = 1000) -> list[dict[str, Any]]:
        if arrow_table.num_rows == 0:
            return []

        conn = duckdb.connect()
        try:
            conn.register("events", arrow_table)
            query = _WORK_QUERY.format(max_retries=self._max_retries, limit=limit)
            result = conn.execute(query)
            columns = [desc[0] for desc in result.description]
            rows = result.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            conn.close()
