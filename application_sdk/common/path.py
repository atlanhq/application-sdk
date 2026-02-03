import os
import sys
from pathlib import Path
from typing import Union

from application_sdk.constants import WINDOWS_EXTENDED_PATH_PREFIX


def convert_to_extended_path(path: Union[str, Path]) -> str:
    """
    Robust conversion to Windows extended-length path ({WINDOWS_EXTENDED_PATH_PREFIX}).

    On Windows, this prefixes the path with {WINDOWS_EXTENDED_PATH_PREFIX} to bypass the 260-character limit.
    It ensures the path is absolute and uses backslashes.
    On non-Windows platforms, it returns the path as a string.

    Args:
        path: The path to convert (str or Path object).

    Returns:
        Optional[str]: The converted path string, or None if input is empty.
    """
    if not path:
        raise ValueError("Path cannot be empty")

    path_str = str(path)

    if sys.platform != "win32":
        return path_str

    if path_str.startswith(WINDOWS_EXTENDED_PATH_PREFIX):
        return path_str

    # Use os.path.abspath for better Windows reliability than Path.absolute()
    # It also handles normalization of separators to backslashes
    abs_path = os.path.abspath(path_str)

    return f"{WINDOWS_EXTENDED_PATH_PREFIX}{abs_path}"
