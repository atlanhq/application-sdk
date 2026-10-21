"""Unit tests for convert_to_extended_path function."""

from collections.abc import Iterator
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.common import path as path_mod
from application_sdk.common.errors import PathEmptyError
from application_sdk.common.path import convert_to_extended_path
from application_sdk.constants import WINDOWS_EXTENDED_PATH_PREFIX
from tests.unit.private_stdlib import private_stdlib


@pytest.fixture
def local_sys() -> Iterator[ModuleType]:
    """The path module's private ``sys``: setting ``platform`` on it leaves the
    worker's real ``sys.platform`` alone."""
    with private_stdlib(path_mod, "sys") as local:
        yield local


@pytest.fixture
def mock_abspath() -> Iterator[MagicMock]:
    """``os.path.abspath`` as the path module sees it, and only it."""
    with (
        private_stdlib(path_mod, "os") as local_os,
        private_stdlib(local_os, "path") as local_os_path,
        patch.object(local_os_path, "abspath") as mock,
    ):
        yield mock


class TestConvertToExtendedPath:
    """Test suite for convert_to_extended_path function."""

    def test_raises_value_error_for_empty_path(self) -> None:
        """Test that empty path raises PathEmptyError."""
        with pytest.raises(PathEmptyError):
            convert_to_extended_path("")

    def test_returns_path_as_is_on_non_windows(self, local_sys: ModuleType) -> None:
        """Test that path is returned unchanged on non-Windows platforms."""
        local_sys.platform = "darwin"
        result = convert_to_extended_path("/some/unix/path")
        assert result == "/some/unix/path"

    def test_adds_prefix_on_windows(
        self, local_sys: ModuleType, mock_abspath: MagicMock
    ) -> None:
        """Test that Windows paths get the extended-length prefix."""
        local_sys.platform = "win32"
        mock_abspath.return_value = "C:\\Users\\test\\file.txt"

        result = convert_to_extended_path("C:\\Users\\test\\file.txt")

        assert result == f"{WINDOWS_EXTENDED_PATH_PREFIX}C:\\Users\\test\\file.txt"

    def test_does_not_double_prefix_on_windows(self, local_sys: ModuleType) -> None:
        """Test that already-prefixed paths are not double-prefixed."""
        local_sys.platform = "win32"
        already_prefixed = f"{WINDOWS_EXTENDED_PATH_PREFIX}C:\\Users\\test\\file.txt"

        result = convert_to_extended_path(already_prefixed)

        assert result == already_prefixed
