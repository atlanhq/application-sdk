import os

from application_sdk.constants import TEMPORARY_PATH


def get_object_store_prefix(path: str) -> str:
    """Get the object store prefix for the path.

    This function handles two types of paths:
    1. Paths under TEMPORARY_PATH - converts them to relative object store paths
    2. User-provided paths - returns them as-is (already relative object store paths)

    Args:
        path: The path to convert to object store prefix.

    Returns:
        The object store prefix for the path.

    Examples:
        >>> # Temporary path case
        >>> get_object_store_prefix("./local/tmp/artifacts/apps/appName/workflows/wf-123/run-456")
        "artifacts/apps/appName/workflows/wf-123/run-456"

        >>> # User-provided path case
        >>> get_object_store_prefix("datasets/sales/2024/")
        "datasets/sales/2024"
    """
    # Normalize paths for comparison
    abs_path = os.path.abspath(path)
    abs_temp_path = os.path.abspath(TEMPORARY_PATH)

    # Check if path is under TEMPORARY_PATH
    try:
        # Use os.path.commonpath to properly check if path is under temp directory
        # This prevents false positives like '/tmp/local123' matching '/tmp/local'
        common_path = os.path.commonpath([abs_path, abs_temp_path])
        if common_path == abs_temp_path:
            # Path is under temp directory, convert to relative object store path
            relative_path = os.path.relpath(abs_path, abs_temp_path)
            # Normalize path separators to forward slashes for object store
            return relative_path.replace(os.path.sep, "/")
        else:
            # Path is already a relative object store path, return as-is
            return path.strip("/")
    except ValueError:
        # os.path.commonpath or os.path.relpath can raise ValueError on Windows with different drives
        # In this case, treat as user-provided path, return as-is
        return path.strip("/")
