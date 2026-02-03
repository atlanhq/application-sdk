import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, List, Optional, Union

from application_sdk.common.path import convert_to_extended_path


class SafeFileOps:
    """Safe file operations with Windows extended-length path support."""

    @staticmethod
    def rename(src: Union[str, Path], dst: Union[str, Path]) -> None:
        """Safely rename a file or directory, supporting long paths on Windows."""
        os.rename(convert_to_extended_path(src), convert_to_extended_path(dst))

    @staticmethod
    def remove(path: Union[str, Path]) -> None:
        """Safely remove a file, supporting long paths on Windows."""
        os.remove(convert_to_extended_path(path))

    @staticmethod
    def unlink(path: Union[str, Path], missing_ok: bool = False) -> None:
        """Safely unlink a file, supporting long paths on Windows."""
        try:
            os.unlink(convert_to_extended_path(path))
        except FileNotFoundError:
            if not missing_ok:
                raise

    @staticmethod
    def makedirs(
        name: Union[str, Path], mode: int = 0o777, exist_ok: bool = False
    ) -> None:
        """Safely create directories, supporting long paths on Windows."""
        os.makedirs(convert_to_extended_path(name), mode=mode, exist_ok=exist_ok)

    @staticmethod
    def mkdir(path: Union[str, Path], mode: int = 0o777) -> None:
        """Safely create a directory, supporting long paths on Windows."""
        os.mkdir(convert_to_extended_path(path), mode=mode)

    @staticmethod
    def rmdir(path: Union[str, Path]) -> None:
        """Safely remove a directory, supporting long paths on Windows."""
        os.rmdir(convert_to_extended_path(path))

    @staticmethod
    def exists(path: Union[str, Path]) -> bool:
        """Safely check if a path exists, supporting long paths on Windows."""
        return os.path.exists(convert_to_extended_path(path))

    @staticmethod
    def isfile(path: Union[str, Path]) -> bool:
        """Safely check if a path is a file, supporting long paths on Windows."""
        return os.path.isfile(convert_to_extended_path(path))

    @staticmethod
    def isdir(path: Union[str, Path]) -> bool:
        """Safely check if a path is a directory, supporting long paths on Windows."""
        return os.path.isdir(convert_to_extended_path(path))

    @staticmethod
    def rmtree(
        path: Union[str, Path],
        ignore_errors: bool = False,
        onerror: Optional[Any] = None,
    ) -> None:
        """Safely remove a directory tree, supporting long paths on Windows."""
        shutil.rmtree(
            convert_to_extended_path(path), ignore_errors=ignore_errors, onerror=onerror
        )

    @staticmethod
    def copy(
        src: Union[str, Path], dst: Union[str, Path], follow_symlinks: bool = True
    ) -> Union[str, Path]:
        """Safely copy a file, supporting long paths on Windows."""
        return shutil.copy(
            convert_to_extended_path(src),
            convert_to_extended_path(dst),
            follow_symlinks=follow_symlinks,
        )

    @staticmethod
    def move(src: Union[str, Path], dst: Union[str, Path]) -> Union[str, Path]:
        """Safely move a file or directory, supporting long paths on Windows."""
        return shutil.move(convert_to_extended_path(src), convert_to_extended_path(dst))

    @staticmethod
    @contextmanager
    def open(
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
            convert_to_extended_path(file),
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

    @staticmethod
    def listdir(path: Union[str, Path]) -> List[str]:
        """Safely list directory contents, supporting long paths on Windows."""
        return os.listdir(convert_to_extended_path(path))
