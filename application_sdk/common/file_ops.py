import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional, Union

from application_sdk.common.path import to_extended_path


def safe_rename(src: Union[str, Path], dst: Union[str, Path]) -> None:
    """Safely rename a file or directory, supporting long paths on Windows."""
    os.rename(to_extended_path(src), to_extended_path(dst))


def safe_remove(path: Union[str, Path]) -> None:
    """Safely remove a file, supporting long paths on Windows."""
    os.remove(to_extended_path(path))


def safe_unlink(path: Union[str, Path], missing_ok: bool = False) -> None:
    """Safely unlink a file, supporting long paths on Windows."""
    try:
        os.unlink(to_extended_path(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def safe_makedirs(
    name: Union[str, Path], mode: int = 0o777, exist_ok: bool = False
) -> None:
    """Safely create directories, supporting long paths on Windows."""
    os.makedirs(to_extended_path(name), mode=mode, exist_ok=exist_ok)


def safe_mkdir(path: Union[str, Path], mode: int = 0o777) -> None:
    """Safely create a directory, supporting long paths on Windows."""
    os.mkdir(to_extended_path(path), mode=mode)


def safe_rmdir(path: Union[str, Path]) -> None:
    """Safely remove a directory, supporting long paths on Windows."""
    os.rmdir(to_extended_path(path))


def safe_exists(path: Union[str, Path]) -> bool:
    """Safely check if a path exists, supporting long paths on Windows."""
    return os.path.exists(to_extended_path(path))


def safe_isfile(path: Union[str, Path]) -> bool:
    """Safely check if a path is a file, supporting long paths on Windows."""
    return os.path.isfile(to_extended_path(path))


def safe_isdir(path: Union[str, Path]) -> bool:
    """Safely check if a path is a directory, supporting long paths on Windows."""
    return os.path.isdir(to_extended_path(path))


def safe_rmtree(
    path: Union[str, Path], ignore_errors: bool = False, onerror: Optional[Any] = None
) -> None:
    """Safely remove a directory tree, supporting long paths on Windows."""
    shutil.rmtree(to_extended_path(path), ignore_errors=ignore_errors, onerror=onerror)


def safe_copy(
    src: Union[str, Path], dst: Union[str, Path], follow_symlinks: bool = True
) -> Union[str, Path]:
    """Safely copy a file, supporting long paths on Windows."""
    return shutil.copy(
        to_extended_path(src), to_extended_path(dst), follow_symlinks=follow_symlinks
    )


def safe_move(src: Union[str, Path], dst: Union[str, Path]) -> Union[str, Path]:
    """Safely move a file or directory, supporting long paths on Windows."""
    return shutil.move(to_extended_path(src), to_extended_path(dst))


@contextmanager
def safe_open(
    file: Union[str, Path],
    mode: str = "r",
    buffering: int = -1,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
    newline: Optional[str] = None,
    closefd: bool = True,
    opener: Optional[Any] = None,
):
    """Safely open a file, supporting long paths on Windows."""
    f = open(
        to_extended_path(file),
        mode=mode,
        buffering=buffering,
        encoding=encoding,
        errors=errors,
        newline=newline,
        closefd=closefd,
        opener=opener,
    )
    try:
        yield f
    finally:
        f.close()


def safe_listdir(path: Union[str, Path]) -> List[str]:
    """Safely list directory contents, supporting long paths on Windows."""
    return os.listdir(to_extended_path(path))
