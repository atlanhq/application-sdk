import functools
import logging
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, TypeVar, cast

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore

logger = AtlanLoggerAdapter(logging.getLogger(__name__))

T = TypeVar("T")


def incremental_query_batching(
    workflow_id: str,
    query: str,
    timestamp_column: str,
    chunk_size: int,
    sql_ranged_replace_from: str,
    sql_ranged_replace_to: str,
    ranged_sql_start_key: str,
    ranged_sql_end_key: str,
    default_start_time: Optional[datetime] = None,
) -> Callable[
    [Callable[..., AsyncIterator[Dict[str, Any]]]],
    Callable[..., AsyncIterator[Dict[str, Any]]],
]:
    """
    A decorator that handles incremental query batching with state management.

    Args:
        workflow_id: Unique identifier for the workflow
        query: The SQL query to parallelize
        timestamp_column: Column name containing the timestamp
        chunk_size: Number of records per chunk
        sql_ranged_replace_from: Original SQL fragment to replace
        sql_ranged_replace_to: SQL fragment with range placeholders
        ranged_sql_start_key: Placeholder for range start timestamp
        ranged_sql_end_key: Placeholder for range end timestamp
        default_start_time: Default start time if no previous state exists
    """

    def decorator(
        func: Callable[..., AsyncIterator[Dict[str, Any]]],
    ) -> Callable[..., AsyncIterator[Dict[str, Any]]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Dict[str, Any]]:
            try:
                # Get last processed timestamp from StateStore
                try:
                    last_processed_timestamp = (
                        StateStore.extract_last_processed_timestamp(workflow_id)
                    )
                    if last_processed_timestamp:
                        start_time_epoch = int(
                            last_processed_timestamp.timestamp() * 1000
                        )
                        logger.info(
                            f"Using last processed timestamp: {last_processed_timestamp}"
                        )
                    else:
                        start_time_epoch = int(
                            (
                                default_start_time
                                or datetime.now() - timedelta(days=14)
                            ).timestamp()
                            * 1000
                        )
                        logger.info(
                            f"Using default start time: {datetime.fromtimestamp(start_time_epoch/1000)}"
                        )
                except Exception as e:
                    logger.info(f"No last processed timestamp found: {e}")
                    start_time_epoch = int(
                        (
                            default_start_time or datetime.now() - timedelta(days=14)
                        ).timestamp()
                        * 1000
                    )
                    logger.info(
                        f"Using default start time: {datetime.fromtimestamp(start_time_epoch/1000)}"
                    )

                # Format query with start time
                formatted_query = query.replace(
                    ranged_sql_start_key, str(start_time_epoch)
                )

                # Initialize variables for chunking
                parallel_markers: List[Dict[str, Any]] = []
                chunk_start_marker: Optional[str] = None
                chunk_end_marker: Optional[str] = None
                record_count = 0
                last_marker: Optional[str] = None

                # Execute the decorated function to get query results
                async for result_batch in func(*args, **kwargs):
                    for row in result_batch:
                        if (
                            not isinstance(row, dict)
                            or timestamp_column.lower() not in row
                        ):
                            continue

                        timestamp_value = cast(
                            Optional[datetime], row.get(timestamp_column.lower())
                        )
                        if not isinstance(timestamp_value, datetime):
                            continue

                        new_marker = str(int(timestamp_value.timestamp() * 1000))

                        if last_marker == new_marker:
                            logger.info("Skipping duplicate start time")
                            record_count += 1
                            continue

                        if not chunk_start_marker:
                            chunk_start_marker = new_marker
                        chunk_end_marker = new_marker
                        record_count += 1
                        last_marker = new_marker

                        if (
                            record_count >= chunk_size
                            and chunk_start_marker
                            and chunk_end_marker
                        ):
                            _create_chunked_query(
                                query=formatted_query,
                                start_marker=chunk_start_marker,
                                end_marker=chunk_end_marker,
                                parallel_markers=parallel_markers,
                                record_count=record_count,
                                sql_ranged_replace_from=sql_ranged_replace_from,
                                sql_ranged_replace_to=sql_ranged_replace_to,
                                ranged_sql_start_key=ranged_sql_start_key,
                                ranged_sql_end_key=ranged_sql_end_key,
                            )
                            record_count = 0
                            chunk_start_marker = None
                            chunk_end_marker = None

                    # Yield the original result batch to maintain the function's contract
                    yield result_batch

                # Handle remaining records
                if record_count > 0 and chunk_start_marker and chunk_end_marker:
                    _create_chunked_query(
                        query=formatted_query,
                        start_marker=chunk_start_marker,
                        end_marker=chunk_end_marker,
                        parallel_markers=parallel_markers,
                        record_count=record_count,
                        sql_ranged_replace_from=sql_ranged_replace_from,
                        sql_ranged_replace_to=sql_ranged_replace_to,
                        ranged_sql_start_key=ranged_sql_start_key,
                        ranged_sql_end_key=ranged_sql_end_key,
                    )

                # Store the latest timestamp
                if chunk_end_marker:
                    latest_datetime = datetime.fromtimestamp(
                        int(chunk_end_marker) / 1000
                    )
                    StateStore.store_last_processed_timestamp(
                        latest_datetime, workflow_id
                    )
                    logger.info(f"Stored last processed timestamp: {latest_datetime}")

                # Store parallel markers in StateStore
                StateStore.store_configuration(
                    f"parallel_markers_{workflow_id}", {"markers": parallel_markers}
                )
                logger.info(
                    f"Stored {len(parallel_markers)} query chunks in state store"
                )

            except Exception as e:
                logger.error(f"Error in incremental query batching: {str(e)}")
                raise

        return wrapper

    return decorator


def _create_chunked_query(
    query: str,
    start_marker: str,
    end_marker: str,
    parallel_markers: List[Dict[str, Any]],
    record_count: int,
    sql_ranged_replace_from: str,
    sql_ranged_replace_to: str,
    ranged_sql_start_key: str,
    ranged_sql_end_key: str,
) -> None:
    """Helper function to create a chunked query with time range parameters."""
    chunked_sql = query.replace(
        sql_ranged_replace_from,
        sql_ranged_replace_to.replace(ranged_sql_start_key, start_marker).replace(
            ranged_sql_end_key, end_marker
        ),
    )

    logger.info(
        f"Processed {record_count} records in chunk {len(parallel_markers)}, "
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
