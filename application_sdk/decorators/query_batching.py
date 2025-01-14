import functools
import logging
from datetime import datetime, timedelta
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    TypeVar,
    Union,
    cast,
)

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore

logger = AtlanLoggerAdapter(logging.getLogger(__name__))

T = TypeVar("T")
AsyncFunc = TypeVar("AsyncFunc", bound=Callable[..., Awaitable[Any]])
AsyncIterFunc = TypeVar(
    "AsyncIterFunc", bound=Callable[..., AsyncIterator[Dict[str, Any]]]
)


def incremental_query_batching(
    workflow_id: str | Callable[..., str],
    query: str | Callable[..., str],
    timestamp_column: str | Callable[..., str],
    chunk_size: int | Callable[..., int],
    sql_ranged_replace_from: str | Callable[..., str],
    sql_ranged_replace_to: str | Callable[..., str],
    ranged_sql_start_key: str | Callable[..., str],
    ranged_sql_end_key: str | Callable[..., str],
    default_start_time: Optional[datetime] = None,
) -> Callable[[Union[AsyncFunc, AsyncIterFunc]], Union[AsyncFunc, AsyncIterFunc]]:
    """
    A decorator that handles incremental query batching with state management.
    Can be applied to both async iterator functions and regular coroutine functions.

    Args:
        workflow_id: Workflow identifier or function returning it
        query: SQL query or function returning it
        timestamp_column: Column name or function returning it
        chunk_size: Chunk size or function returning it
        sql_ranged_replace_from: Original SQL fragment or function returning it
        sql_ranged_replace_to: SQL fragment with placeholders or function returning it
        ranged_sql_start_key: Start key placeholder or function returning it
        ranged_sql_end_key: End key placeholder or function returning it
        default_start_time: Default start time if no previous state exists
    """

    def resolve_param(
        param: Any | Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Helper to resolve parameters that can be either values or callables"""
        if callable(param):
            return param(*args, **kwargs)
        return param

    def decorator(
        func: Union[AsyncFunc, AsyncIterFunc],
    ) -> Union[AsyncFunc, AsyncIterFunc]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                # Resolve all parameters
                resolved_workflow_id = resolve_param(workflow_id, *args, **kwargs)
                resolved_query = resolve_param(query, *args, **kwargs)
                resolved_timestamp_column = resolve_param(
                    timestamp_column, *args, **kwargs
                )
                resolved_chunk_size = resolve_param(chunk_size, *args, **kwargs)
                resolved_sql_ranged_replace_from = resolve_param(
                    sql_ranged_replace_from, *args, **kwargs
                )
                resolved_sql_ranged_replace_to = resolve_param(
                    sql_ranged_replace_to, *args, **kwargs
                )
                resolved_ranged_sql_start_key = resolve_param(
                    ranged_sql_start_key, *args, **kwargs
                )
                resolved_ranged_sql_end_key = resolve_param(
                    ranged_sql_end_key, *args, **kwargs
                )

                # Get last processed timestamp from StateStore
                try:
                    last_processed_timestamp = (
                        StateStore.extract_last_processed_timestamp(
                            resolved_workflow_id
                        )
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
                formatted_query = resolved_query.replace(
                    resolved_ranged_sql_start_key, str(start_time_epoch)
                )

                # Initialize variables for chunking
                parallel_markers: List[Dict[str, Any]] = []
                chunk_start_marker: Optional[str] = None
                chunk_end_marker: Optional[str] = None
                record_count = 0
                last_marker: Optional[str] = None

                # Execute the decorated function
                result = await func(*args, **kwargs)

                # Handle async iterator functions
                if isinstance(result, AsyncIterator):
                    async for batch in result:
                        if isinstance(batch, dict):
                            await process_batch(
                                cast(Dict[str, Any], batch),
                                resolved_timestamp_column,
                                resolved_chunk_size,
                                formatted_query,
                                resolved_sql_ranged_replace_from,
                                resolved_sql_ranged_replace_to,
                                resolved_ranged_sql_start_key,
                                resolved_ranged_sql_end_key,
                                parallel_markers,
                                chunk_start_marker,
                                chunk_end_marker,
                                record_count,
                                last_marker,
                            )
                            yield batch

                # Store state after processing all batches
                if chunk_end_marker:
                    latest_datetime = datetime.fromtimestamp(
                        int(chunk_end_marker) / 1000
                    )
                    StateStore.store_last_processed_timestamp(
                        latest_datetime, resolved_workflow_id
                    )
                    logger.info(f"Stored last processed timestamp: {latest_datetime}")

                StateStore.store_configuration(
                    f"parallel_markers_{resolved_workflow_id}",
                    {"markers": parallel_markers},
                )
                logger.info(
                    f"Stored {len(parallel_markers)} query chunks in state store"
                )

                # For non-iterator functions, return the original result
                if not isinstance(result, AsyncIterator):
                    return result

            except Exception as e:
                logger.error(f"Error in incremental query batching: {str(e)}")
                raise

        return cast(Union[AsyncFunc, AsyncIterFunc], wrapper)

    return decorator


async def process_batch(
    result_batch: Dict[str, Any],
    timestamp_column: str,
    chunk_size: int,
    formatted_query: str,
    sql_ranged_replace_from: str,
    sql_ranged_replace_to: str,
    ranged_sql_start_key: str,
    ranged_sql_end_key: str,
    parallel_markers: List[Dict[str, Any]],
    chunk_start_marker: Optional[str],
    chunk_end_marker: Optional[str],
    record_count: int,
    last_marker: Optional[str],
) -> None:
    """Helper function to process a batch of results and update markers."""
    timestamp_value = result_batch.get(timestamp_column.lower())
    if not isinstance(timestamp_value, datetime):
        return

    new_marker = str(int(timestamp_value.timestamp() * 1000))

    if last_marker == new_marker:
        logger.info("Skipping duplicate start time")
        record_count += 1
        return

    if not chunk_start_marker:
        chunk_start_marker = new_marker
    chunk_end_marker = new_marker
    record_count += 1
    last_marker = new_marker

    if record_count >= chunk_size and chunk_start_marker and chunk_end_marker:
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
