import asyncio
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

                # Initialize parallel markers list
                parallel_markers: List[Dict[str, Any]] = []

                # Create time-based chunks
                current_time = datetime.now()
                current_time_epoch = int(current_time.timestamp() * 1000)

                chunk_start = start_time_epoch
                while chunk_start < current_time_epoch:
                    chunk_end = min(
                        chunk_start + (resolved_chunk_size * 1000), current_time_epoch
                    )

                    chunked_sql = resolved_query.replace(
                        resolved_sql_ranged_replace_from,
                        resolved_sql_ranged_replace_to.replace(
                            resolved_ranged_sql_start_key, str(chunk_start)
                        ).replace(resolved_ranged_sql_end_key, str(chunk_end)),
                    )

                    parallel_markers.append(
                        {
                            "sql": chunked_sql,
                            "start": str(chunk_start),
                            "end": str(chunk_end),
                        }
                    )

                    chunk_start = chunk_end + 1

                # Store chunks in state store
                StateStore.store_configuration(
                    f"parallel_markers_{resolved_workflow_id}",
                    {"markers": parallel_markers},
                )
                logger.info(
                    f"Stored {len(parallel_markers)} query chunks in state store"
                )

                # Execute the decorated function with the chunked queries
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                return func(*args, **kwargs)

            except Exception as e:
                logger.error(f"Error in incremental query batching: {str(e)}")
                raise

        return cast(Union[AsyncFunc, AsyncIterFunc], wrapper)

    return decorator
