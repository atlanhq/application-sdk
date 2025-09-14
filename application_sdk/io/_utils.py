"""Shared utilities for data input/output operations.

This module contains utility functions that are used across different writer
implementations. These utilities handle common data processing tasks like
datetime conversion, null field processing, and DataFrame type detection.
"""

import inspect
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncGenerator, Generator, List, Optional, Union

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


def convert_datetime_to_epoch(data: Any) -> Any:
    """Convert datetime objects to epoch timestamps in milliseconds.

    This function recursively processes data structures (dicts, lists) and converts
    any datetime objects to epoch timestamps in milliseconds. This is commonly
    needed for JSON serialization where datetime objects need to be represented
    as numbers.

    Args:
        data: The data to convert (can be datetime, dict, list, or any other type)

    Returns:
        The converted data with datetime fields as epoch timestamps in milliseconds

    Example:
        >>> from datetime import datetime
        >>> dt = datetime(2024, 1, 1, 12, 0, 0)
        >>> convert_datetime_to_epoch(dt)
        1704110400000
        >>> convert_datetime_to_epoch({"created": dt, "name": "test"})
        {"created": 1704110400000, "name": "test"}
    """
    if isinstance(data, datetime):
        return int(data.timestamp() * 1000)
    elif isinstance(data, dict):
        return {k: convert_datetime_to_epoch(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_datetime_to_epoch(item) for item in data]
    return data


def process_null_fields(
    obj: Any,
    preserve_fields: Optional[List[str]] = None,
    null_to_empty_dict_fields: Optional[List[str]] = None,
) -> Any:
    """Process null values in data structures according to specified rules.

    This function recursively processes dictionaries and removes null values,
    with special handling for fields that should be preserved or converted
    to empty dictionaries.

    Args:
        obj: The object to process (dict, list, or other value)
        preserve_fields: List of field names that should be preserved even if null
        null_to_empty_dict_fields: List of field names that should be converted
            from null to empty dict

    Returns:
        The processed object with null values handled according to rules

    Example:
        >>> data = {"id": 1, "name": None, "metadata": None, "active": True}
        >>> process_null_fields(
        ...     data,
        ...     preserve_fields=["name"],
        ...     null_to_empty_dict_fields=["metadata"]
        ... )
        {"id": 1, "name": None, "metadata": {}, "active": True}
    """
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # Handle null fields that should be converted to empty dicts
            if k in (null_to_empty_dict_fields or []) and v is None:
                result[k] = {}
                continue

            # Process the value recursively
            processed_value = process_null_fields(
                v, preserve_fields, null_to_empty_dict_fields
            )

            # Keep the field if it's in preserve_fields or has a non-None processed value
            if k in (preserve_fields or []) or processed_value is not None:
                result[k] = processed_value

        return result
    return obj


def is_daft_dataframe(obj: Any) -> bool:
    """Check if an object is a Daft DataFrame.

    Args:
        obj: Object to check

    Returns:
        True if the object is a Daft DataFrame, False otherwise
    """
    return hasattr(obj, "iter_rows") and hasattr(obj, "count_rows")


def is_pandas_dataframe(obj: Any) -> bool:
    """Check if an object is a Pandas DataFrame.

    Args:
        obj: Object to check

    Returns:
        True if the object is a Pandas DataFrame, False otherwise
    """
    return hasattr(obj, "to_json") and hasattr(obj, "iloc")


def is_single_dataframe(obj: Any) -> bool:
    """Check if an object is a single DataFrame (pandas or daft).

    Args:
        obj: Object to check

    Returns:
        True if the object is a single DataFrame, False otherwise
    """
    return is_pandas_dataframe(obj) or is_daft_dataframe(obj)


def is_empty_dataframe(dataframe: Union["pd.DataFrame", "daft.DataFrame"]) -> bool:
    """Check if a DataFrame is empty.

    Args:
        dataframe: DataFrame to check (pandas or daft)

    Returns:
        True if the DataFrame is empty, False otherwise
    """
    if is_pandas_dataframe(dataframe):
        return len(dataframe) == 0
    elif is_daft_dataframe(dataframe):
        return dataframe.count_rows() == 0
    else:
        return True


async def normalize_to_async_iterator(
    data: Union[
        "pd.DataFrame",
        "daft.DataFrame",
        Generator["pd.DataFrame", None, None],
        AsyncGenerator["pd.DataFrame", None],
        Generator["daft.DataFrame", None, None],
        AsyncGenerator["daft.DataFrame", None],
    ],
) -> AsyncGenerator[Union["pd.DataFrame", "daft.DataFrame"], None]:
    """Normalize any input type to an async iterator of DataFrames.

    This function takes various input types (single DataFrames, sync generators,
    async generators) and converts them to a consistent async iterator interface.
    This allows the write methods to handle all input types uniformly.

    Args:
        data: Input data in various formats

    Yields:
        DataFrames from the input data

    Raises:
        ValueError: If the input type is not supported

    Example:
        >>> async for df in normalize_to_async_iterator(single_dataframe):
        ...     print(f"Processing {len(df)} rows")
    """
    if is_single_dataframe(data):
        # Single DataFrame -> yield once
        if not is_empty_dataframe(data):
            yield data
    elif inspect.isasyncgen(data):
        # Already async generator -> iterate through it
        async for dataframe in data:
            if not is_empty_dataframe(dataframe):
                yield dataframe
    elif inspect.isgenerator(data):
        # Sync generator -> convert to async
        for dataframe in data:
            if not is_empty_dataframe(dataframe):
                yield dataframe
    else:
        raise ValueError(f"Unsupported data type: {type(data)}")


def estimate_json_size(dataframe: Union["pd.DataFrame", "daft.DataFrame"]) -> int:
    """Estimate the JSON file size of a DataFrame by sampling records.

    Args:
        dataframe: DataFrame to estimate size for

    Returns:
        Estimated file size in bytes
    """
    if is_empty_dataframe(dataframe):
        return 0

    if is_pandas_dataframe(dataframe):
        # Sample up to 10 records for pandas
        sample_size = min(10, len(dataframe))
        sample = dataframe.head(sample_size)
        sample_json = sample.to_json(orient="records", lines=True)
        if sample_json:
            avg_record_size = len(sample_json.encode("utf-8")) / sample_size
            return int(avg_record_size * len(dataframe))
    elif is_daft_dataframe(dataframe):
        # For daft, estimate based on row count (approximate)
        # This is a rough estimate since we can't easily sample daft DataFrames
        row_count = dataframe.count_rows()
        estimated_bytes_per_row = 100  # Conservative estimate
        return row_count * estimated_bytes_per_row

    return 0
