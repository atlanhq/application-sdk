import os
from unittest.mock import AsyncMock, Mock, patch

import pandas as pd
import pytest

from application_sdk.activities.query_extraction.sql import SQLQueryExtractionActivities


@pytest.mark.asyncio
async def test_fetch_queries_large_dataframe_persists_all_rows(tmp_path):
    """fetch_queries() should persist all rows for large single write() payloads."""
    total_rows = 10037
    dataframe = pd.DataFrame(
        {
            "id": range(total_rows),
            "query_text": [f"select {i}" for i in range(total_rows)],
        }
    )

    activities = SQLQueryExtractionActivities()
    activities.fetch_queries_sql = (
        "SELECT * FROM query_log WHERE {sql_replace_from} "
        "AND db='{database_name_cleaned}' AND schema='{schema_name_cleaned}'"
    )

    sql_client = Mock()
    sql_client.get_results = AsyncMock(return_value=dataframe)
    state = Mock(sql_client=sql_client)

    workflow_args = {
        "output_path": str(tmp_path),
        "start_marker": "1000",
        "end_marker": "2000",
        "miner_args": {
            "database_name_cleaned": "db1",
            "schema_name_cleaned": "public",
            "timestamp_column": "event_time",
            "chunk_size": 6000,
            "current_marker": 0,
            "sql_replace_from": "[RANGE_CLAUSE]",
            "sql_replace_to": "event_ms >= [START_MARKER] and event_ms < [END_MARKER]",
            "ranged_sql_start_key": "[START_MARKER]",
            "ranged_sql_end_key": "[END_MARKER]",
        },
    }

    with patch.object(activities, "_get_state", AsyncMock(return_value=state)), patch(
        "application_sdk.io.ObjectStore.upload_file", new_callable=AsyncMock
    ):
        await activities.fetch_queries(workflow_args)

    sql_client.get_results.assert_awaited_once_with(
        "SELECT * FROM query_log WHERE event_ms >= 1000 and event_ms < 2000 "
        "AND db='db1' AND schema='public'"
    )

    output_dir = os.path.join(str(tmp_path), "raw", "query")
    parquet_files = sorted(f for f in os.listdir(output_dir) if f.endswith(".parquet"))
    persisted_rows = sum(
        len(pd.read_parquet(os.path.join(output_dir, file_name)))
        for file_name in parquet_files
    )

    assert persisted_rows == total_rows
    assert len(parquet_files) > 1
