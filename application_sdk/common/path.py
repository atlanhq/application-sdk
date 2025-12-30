import os
import sys
from pathlib import Path
from typing import Union


def to_extended_path(path: Union[str, Path]) -> str:
    """
    Robust conversion to Windows extended-length path (\\\\?\\).

    On Windows, this prefixes the path with \\\\?\\ to bypass the 260-character limit.
    It ensures the path is absolute and uses backslashes.
    On non-Windows platforms, it returns the path as a string.

    Args:
        path: The path to convert (str or Path object).

    Returns:
        str: The converted path string.
    """
    if not path:
        return ""

    path_str = str(path)

    if sys.platform != "win32":
        return path_str

    if path_str.startswith("\\\\?\\"):
        return path_str

    # Use os.path.abspath for better Windows reliability than Path.absolute()
    # It also handles normalization of separators to backslashes
    abs_path = os.path.abspath(path_str)

    return f"\\\\?\\{abs_path}"
