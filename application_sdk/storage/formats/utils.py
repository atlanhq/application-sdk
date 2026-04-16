import glob
import os
import uuid
import warnings
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional, Union

from application_sdk.common.error_codes import IOError
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage.batch import download_prefix as _download_prefix
from application_sdk.storage.ops import download_file as _download_file
from application_sdk.storage.ops import normalize_key

JSON_FILE_EXTENSION = ".json"
PARQUET_FILE_EXTENSION = ".parquet"

if TYPE_CHECKING:
    import daft
    import pandas as pd


logger = get_logger(__name__)


def find_local_files_by_extension(
    path: str,
    extension: str,
    file_names: Optional[List[str]] = None,
) -> List[str]:
    """Find local files at the specified local path, optionally filtering by file names.

    Args:
        path (str): Local path to search in (file or directory)
        extension (str): File extension to filter by (e.g., '.parquet', '.json')
        file_names (Optional[List[str]]): List of relative paths or basenames to filter by.
            Supports both plain basenames (``"file1.parquet"``) and relative paths
            containing directories (``"table/chunk-0-part0.parquet"``).

    Returns:
        List[str]: List of matching file paths

    Example:
        >>> find_local_files_by_extension("/data", ".parquet", ["file1.parquet", "file2.parquet"])
        ['file1.parquet', 'file2.parquet']

        >>> find_local_files_by_extension("/data/single.json", ".json")
        ['single.json']
    """
    if os.path.isfile(path) and path.endswith(extension):
        # Single file - return it directly
        return [path]

    elif os.path.isdir(path):
        # Directory - find all files in directory
        all_files = glob.glob(
            os.path.join(path, "**", f"*{extension}"),
            recursive=True,
        )

        # Filter by file names if specified
        if file_names:
            rel_paths_set: set[str] = set()
            basenames_only: set[str] = set()
            for fn in file_names:
                if "/" in fn or (os.sep != "/" and os.sep in fn):
                    # Entry contains a directory component — use precise
                    # relative-path matching (e.g. "table/chunk-0-part0.parquet").
                    # Normalize to forward slashes for cross-platform matching.
                    rel_paths_set.add(fn.replace("\\", "/"))
                else:
                    # Plain basename — use basename matching for backward
                    # compatibility (e.g. "file1.parquet").
                    basenames_only.add(fn)

            def _matches(filepath: str) -> bool:
                # Normalize to forward slashes so Windows paths match too.
                rel = os.path.relpath(filepath, path).replace("\\", "/")
                if rel in rel_paths_set:
                    return True
                if basenames_only and os.path.basename(filepath) in basenames_only:
                    return True
                return False

            return [f for f in all_files if _matches(f)]
        else:
            return all_files

    return []


async def download_files(
    path: str, file_extension: str, file_names: Optional[List[str]] = None
) -> List[str]:
    """Download files from object store if not available locally.

    .. deprecated::
        Use :func:`application_sdk.storage.transfer.download` instead, which
        provides SHA-256 integrity verification and skip-if-exists deduplication.

    Flow:
    1. Check if files exist locally at self.path
    2. If not, try to download from object store
    3. Filter by self.file_names if provided
    4. Return list of file paths for logging purposes

    Returns:
        List[str]: List of file paths

    Raises:
        AttributeError: When the reader class doesn't support file operations or _extension
        IOError: When no files found locally or in object store
    """
    warnings.warn(
        "download_files() is deprecated. Use application_sdk.storage.transfer.download() "
        "instead, which provides SHA-256 integrity verification and skip-if-exists deduplication.",
        DeprecationWarning,
        stacklevel=2,
    )
    # Step 1: Check if files exist locally
    local_files: List[str] = find_local_files_by_extension(
        path, file_extension, file_names
    )
    if local_files:
        logger.info(
            "Found files locally",
            file_count=len(local_files),
            file_extension=file_extension,
            path=path,
        )
        return local_files

    # Step 2: Try to download from object store
    logger.info(
        "No local files found, checking object store",
        file_extension=file_extension,
        path=path,
    )

    try:
        # Determine what to download based on path type and filters
        downloaded_paths: List[str] = []

        # Use a unique download directory per invocation to prevent race
        # conditions when multiple activities download files concurrently
        # in the same worker process (e.g. parallel transform_data tasks).
        download_id = uuid.uuid4().hex[:12]
        isolated_tmp = os.path.join(TEMPORARY_PATH, download_id)

        if path.endswith(file_extension):
            # Single file case
            store_key = normalize_key(path)
            destination_path = os.path.join(isolated_tmp, store_key)
            await _download_file(store_key, destination_path)
            downloaded_paths.append(destination_path)

        elif file_names:
            # Directory with specific files - download each individually
            for file_name in file_names:
                file_path = os.path.join(path, file_name)
                store_key = normalize_key(file_path)
                destination_path = os.path.join(isolated_tmp, store_key)
                await _download_file(store_key, destination_path)
                downloaded_paths.append(destination_path)
        else:
            # Download entire directory
            prefix = normalize_key(path)
            dest_dir = os.path.join(isolated_tmp, prefix)
            await _download_prefix(prefix, isolated_tmp, suffix=file_extension)
            found_files = find_local_files_by_extension(
                dest_dir, file_extension, file_names
            )
            downloaded_paths.extend(found_files)

        # Check results
        if downloaded_paths:
            logger.info(
                "Successfully downloaded files from object store",
                file_count=len(downloaded_paths),
                file_extension=file_extension,
            )
            return downloaded_paths
        else:
            raise IOError(
                f"{IOError.OBJECT_STORE_READ_ERROR}: Downloaded from object store but no {file_extension} files found"
            )

    except Exception as e:
        raise IOError(
            f"{IOError.OBJECT_STORE_DOWNLOAD_ERROR}: No {file_extension} files found locally at '{path}' and failed to download from object store. "
            f"Error: {str(e)}"
        ) from e


def estimate_dataframe_record_size(
    dataframe: "pd.DataFrame", file_extension: str
) -> int:
    """Estimate File size of a DataFrame by sampling a few records.

    Args:
        dataframe (pd.DataFrame): The DataFrame to estimate the size of.
        file_extension (str): The extension of the file to estimate the size of.

    Returns:
        int: The estimated size of the DataFrame in bytes.
    """
    if len(dataframe) == 0:
        return 0

    # Sample up to 10 records to estimate average size
    sample_size = min(10, len(dataframe))
    sample = dataframe.head(sample_size)
    compression_factor = 1
    if file_extension == JSON_FILE_EXTENSION:
        sample_file = sample.to_json(orient="records", lines=True)
    elif file_extension == PARQUET_FILE_EXTENSION:
        sample_file = sample.to_parquet(index=False, compression="snappy")
        compression_factor = 0.01
    else:
        raise ValueError(f"Unsupported file extension: {file_extension}")

    if sample_file is not None:
        avg_record_size = len(sample_file) / sample_size * compression_factor
        return int(avg_record_size)

    return 0


def path_gen(
    chunk_count: Optional[int] = None,
    chunk_part: int = 0,
    start_marker: Optional[str] = None,
    end_marker: Optional[str] = None,
    extension: str = ".json",
) -> str:
    """Generate a file path for a chunk.

    Args:
        chunk_start (Optional[int]): Starting index of the chunk, or None for single chunk.
        chunk_count (int): Total number of chunks.
        start_marker (Optional[str]): Start marker for query extraction.
        end_marker (Optional[str]): End marker for query extraction.

    Returns:
        str: Generated file path for the chunk.
    """
    # For Query Extraction - use start and end markers without chunk count
    if start_marker and end_marker:
        return f"{start_marker}_{end_marker}{extension}"

    # For regular chunking - include chunk count
    if chunk_count is None:
        return f"{str(chunk_part)}{extension}"
    else:
        return f"chunk-{str(chunk_count)}-part{str(chunk_part)}{extension}"


def process_null_fields(
    obj: Any,
    preserve_fields: Optional[List[str]] = None,
    null_to_empty_dict_fields: Optional[List[str]] = None,
) -> Any:
    """
    By default the method removes null values from dictionaries and lists.
    Except for the fields specified in preserve_fields.
    And fields in null_to_empty_dict_fields are replaced with empty dict if null.

    Args:
        obj: The object to clean (dict, list, or other value)
        preserve_fields: Optional list of field names that should be preserved even if they contain null values
        null_to_empty_dict_fields: Optional list of field names that should be replaced with empty dict if null

    Returns:
        The cleaned object with null values removed
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


def convert_datetime_to_epoch(data: Any) -> Any:
    """Convert datetime objects to epoch timestamps in milliseconds.

    Args:
        data: The data to convert

    Returns:
        The converted data with datetime fields as epoch timestamps
    """
    if isinstance(data, datetime):
        return int(data.timestamp() * 1000)
    elif isinstance(data, dict):
        return {k: convert_datetime_to_epoch(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_datetime_to_epoch(item) for item in data]
    return data


def is_empty_dataframe(dataframe: Union["pd.DataFrame", "daft.DataFrame"]) -> bool:  # noqa: F821
    """Check if a DataFrame is empty.

    This function determines whether a DataFrame has any rows, supporting both
    pandas and daft DataFrame types. For pandas DataFrames, it uses the `empty`
    property, and for daft DataFrames, it checks if the row count is 0.

    Args:
        dataframe (Union[pd.DataFrame, daft.DataFrame]): The DataFrame to check,
            can be either a pandas DataFrame or a daft DataFrame.

    Returns:
        bool: True if the DataFrame has no rows, False otherwise.

    Note:
        If daft is not available and a daft DataFrame is passed, the function
        will log a warning and return True.
    """
    import pandas as pd

    if isinstance(dataframe, pd.DataFrame):
        return dataframe.empty

    try:
        import daft

        if isinstance(dataframe, daft.DataFrame):
            return dataframe.count_rows() == 0
    except Exception:
        logger.warning("Module daft not found", exc_info=True)
    return True
