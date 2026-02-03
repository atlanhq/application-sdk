"""Unit tests for convert_to_extended_path function."""

from unittest.mock import patch

import pytest

from application_sdk.common.path import convert_to_extended_path
from application_sdk.constants import WINDOWS_EXTENDED_PATH_PREFIX


class TestConvertToExtendedPath:
    """Test suite for convert_to_extended_path function."""

    def test_raises_value_error_for_empty_path(self) -> None:
        """Test that empty path raises ValueError."""
        with pytest.raises(ValueError, match="Path cannot be empty"):
            convert_to_extended_path("")

    def test_returns_path_as_is_on_non_windows(self) -> None:
        """Test that path is returned unchanged on non-Windows platforms."""
        with patch("application_sdk.common.path.sys.platform", "darwin"):
            result = convert_to_extended_path("/some/unix/path")
            assert result == "/some/unix/path"

    @patch("application_sdk.common.path.sys.platform", "win32")
    @patch("application_sdk.common.path.os.path.abspath")
    def test_adds_prefix_on_windows(self, mock_abspath) -> None:
        """Test that Windows paths get the extended-length prefix."""
        mock_abspath.return_value = "C:\\Users\\test\\file.txt"

        result = convert_to_extended_path("C:\\Users\\test\\file.txt")

        assert result == f"{WINDOWS_EXTENDED_PATH_PREFIX}C:\\Users\\test\\file.txt"

    @patch("application_sdk.common.path.sys.platform", "win32")
    def test_does_not_double_prefix_on_windows(self) -> None:
        """Test that already-prefixed paths are not double-prefixed."""
        already_prefixed = f"{WINDOWS_EXTENDED_PATH_PREFIX}C:\\Users\\test\\file.txt"

        result = convert_to_extended_path(already_prefixed)

        assert result == already_prefixed
