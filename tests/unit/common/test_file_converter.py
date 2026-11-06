"""
Unit tests for the file_converter module.

This module contains comprehensive tests for file conversion functionality,
covering all conversion types, error handling, and edge cases.

Test Case Bucketing and Coverage:
+--------------------------------+-------------+----------------------------------------+
| Test Category                  | Test Count  | Coverage Description                   |
+--------------------------------+-------------+----------------------------------------+
| convert_data_files Function    |      6      | Main public interface testing          |
| JSON to Parquet Conversion     |      4      | Success, error, and edge cases         |
| Parquet to JSON Conversion     |      4      | Success, error, and edge cases         |
| Registry Integration           |      2      | Converter registry functionality       |
| Edge Cases & Error Handling   |      3      | Invalid inputs, unknown conversions    |
+--------------------------------+-------------+----------------------------------------+
| Total Test Cases               |     19      | Comprehensive behavioral coverage      |
+--------------------------------+-------------+----------------------------------------+

Key Test Areas:
• Main conversion workflow through convert_data_files
• Individual converter function behavior
• Error handling and recovery
• File path transformation logic
• Registry integration and lookup
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import pytest

from application_sdk.common.errors import FileConverterNotFoundError
from application_sdk.common.file_converter import (
    ConvertFile,
    FileType,
    convert_data_files,
    convert_json_to_parquet,
    convert_parquet_to_json,
    file_converter_registry,
)

# Only mark async tests individually


class TestConvertDataFiles:
    """Test suite for the main convert_data_files function."""

    @pytest.mark.asyncio
    async def test_empty_input_list_returns_empty_list(self):
        """Test that empty input list returns empty list."""
        result = await convert_data_files([], FileType.PARQUET)
        assert result == []

    @pytest.mark.asyncio
    @patch("application_sdk.common.file_converter.file_converter_registry")
    async def test_successful_conversion_single_file(self, mock_registry):
        """Test successful conversion of a single file."""
        # Arrange
        mock_converter = Mock(return_value="/path/output.parquet")
        mock_registry.registry.get.return_value = mock_converter

        input_files = ["/path/input.json"]

        # Act
        result = await convert_data_files(input_files, FileType.PARQUET)

        # Assert
        assert result == ["/path/output.parquet"]
        mock_registry.registry.get.assert_called_once_with(ConvertFile.JSON_TO_PARQUET)
        mock_converter.assert_called_once_with("/path/input.json")

    @pytest.mark.asyncio
    @patch("application_sdk.common.file_converter.file_converter_registry")
    async def test_successful_conversion_multiple_files(self, mock_registry):
        """Test successful conversion of multiple files."""
        # Arrange
        mock_converter = Mock(
            side_effect=[
                "/path/output1.parquet",
                "/path/output2.parquet",
                "/path/output3.parquet",
            ]
        )
        mock_registry.registry.get.return_value = mock_converter

        input_files = ["/path/input1.json", "/path/input2.json", "/path/input3.json"]

        # Act
        result = await convert_data_files(input_files, FileType.PARQUET)

        # Assert
        assert result == [
            "/path/output1.parquet",
            "/path/output2.parquet",
            "/path/output3.parquet",
        ]
        assert mock_converter.call_count == 3

    @pytest.mark.asyncio
    @patch("application_sdk.common.file_converter.file_converter_registry")
    async def test_conversion_with_failed_files_skipped(self, mock_registry):
        """Test that files returning None are skipped from results."""
        # Arrange
        mock_converter = Mock(
            side_effect=[
                "/path/output1.parquet",
                None,  # Failed conversion
                "/path/output3.parquet",
            ]
        )
        mock_registry.registry.get.return_value = mock_converter

        input_files = ["/path/input1.json", "/path/input2.json", "/path/input3.json"]

        # Act
        result = await convert_data_files(input_files, FileType.PARQUET)

        # Assert
        assert result == ["/path/output1.parquet", "/path/output3.parquet"]
        assert mock_converter.call_count == 3

    @pytest.mark.asyncio
    async def test_unsupported_conversion_raises_value_error(self):
        """Test that unsupported conversion raises ValueError when ConvertFile enum construction fails."""
        input_files = ["/path/input.xyz"]

        # Act & Assert - This should raise ValueError when trying to construct ConvertFile enum
        with pytest.raises(ValueError):
            await convert_data_files(input_files, FileType.PARQUET)

    @pytest.mark.asyncio
    @patch("application_sdk.common.file_converter.file_converter_registry")
    async def test_converter_not_found_error(self, mock_registry):
        """Test that when converter function is None, ValueError is raised."""
        # Arrange - registry returns None for unknown converter
        mock_registry.registry.get.return_value = None

        input_files = ["/path/input.json"]

        # Act & Assert - Should raise FileConverterNotFoundError
        with pytest.raises(FileConverterNotFoundError):
            await convert_data_files(input_files, FileType.PARQUET)

    @pytest.mark.asyncio
    @patch("application_sdk.common.file_converter.file_converter_registry")
    async def test_file_extension_detection_from_first_file(self, mock_registry):
        """Test that file extension is detected from the first file in the list."""
        # Arrange
        mock_converter = Mock(return_value="/path/output.json")
        mock_registry.registry.get.return_value = mock_converter

        # Mixed file types - should use extension from first file
        input_files = ["/path/input.parquet", "/path/input.json"]

        # Act
        result = await convert_data_files(input_files, FileType.JSON)

        # Assert - Should look for parquet_to_json converter based on first file
        assert result == ["/path/output.json", "/path/output.json"]
        mock_registry.registry.get.assert_called_once_with(ConvertFile.PARQUET_TO_JSON)


class TestConvertJsonToParquet:
    """Test suite for the convert_json_to_parquet function."""

    def test_successful_json_to_parquet_conversion(self, tmp_path):
        """JSON lines become a Parquet file whose text columns are ``string``.

        pandas 3 reads text as its ``str`` dtype, which pyarrow would write as
        ``large_string``; the converter keeps the type pandas 2 wrote.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        source = tmp_path / "input.json"
        source.write_text('{"id": 1, "name": "a"}\n{"id": 2, "name": "b"}\n')

        result = convert_json_to_parquet(str(source))

        assert result == str(tmp_path / "input.parquet")
        table = pq.read_table(result)
        assert table.schema.field("name").type == pa.string()
        assert table.column("name").to_pylist() == ["a", "b"]
        assert table.column("id").to_pylist() == [1, 2]

    @patch("pandas.read_json")
    def test_json_to_parquet_read_error_returns_none(self, mock_read_json):
        """Test that read errors return None and are logged."""
        # Arrange
        mock_read_json.side_effect = Exception("File read error")

        file_path = "/path/input.json"

        # Act
        with patch("application_sdk.common.file_converter.logger.error") as mock_logger:
            result = convert_json_to_parquet(file_path)

        # Assert
        assert result is None
        mock_logger.assert_called_once()

    def test_json_to_parquet_write_error_returns_none(self, tmp_path):
        """A failing Parquet write returns None and is logged.

        The JSON is real, so conversion succeeds and the failure comes from the
        writer itself rather than from converting a mock.
        """
        source = tmp_path / "input.json"
        source.write_text('{"id": 1, "name": "a"}\n')

        with (
            patch(
                "pyarrow.parquet.write_table", side_effect=OSError("disk full")
            ) as mock_write,
            patch("application_sdk.common.file_converter.logger.error") as mock_logger,
        ):
            result = convert_json_to_parquet(str(source))

        assert result is None
        mock_write.assert_called_once()
        mock_logger.assert_called_once()
        assert not (tmp_path / "input.parquet").exists()

    def test_json_to_parquet_column_filtering(self, tmp_path):
        """A column with no truthy value is dropped from the Parquet output."""
        import pyarrow.parquet as pq

        source = tmp_path / "input.json"
        source.write_text(
            '{"id": 1, "empty": null, "name": "a"}\n'
            '{"id": 2, "empty": null, "name": "b"}\n'
        )

        result = convert_json_to_parquet(str(source))

        assert result is not None
        assert pq.read_schema(result).names == ["id", "name"]


class TestConvertParquetToJson:
    """Test suite for the convert_parquet_to_json function."""

    @patch("pandas.read_parquet")
    def test_successful_parquet_to_json_conversion(self, mock_read_parquet):
        """Test successful conversion from Parquet to JSON."""
        # Arrange
        mock_df = MagicMock()
        mock_read_parquet.return_value = mock_df

        file_path = "/path/input.parquet"
        expected_output = "/path/input.json"

        # Act
        result = convert_parquet_to_json(file_path)

        # Assert
        assert result == expected_output
        mock_read_parquet.assert_called_once_with(file_path)
        mock_df.to_json.assert_called_once_with(
            expected_output, orient="records", lines=True
        )

    @patch("pandas.read_parquet")
    def test_parquet_to_json_read_error_returns_none(self, mock_read_parquet):
        """Test that read errors return None and are logged."""
        # Arrange
        mock_read_parquet.side_effect = Exception("File read error")

        file_path = "/path/input.parquet"

        # Act
        with patch("application_sdk.common.file_converter.logger.error") as mock_logger:
            result = convert_parquet_to_json(file_path)

        # Assert
        assert result is None
        mock_logger.assert_called_once()

    @patch("pandas.read_parquet")
    def test_parquet_to_json_write_error_returns_none(self, mock_read_parquet):
        """Test that write errors return None and are logged."""
        # Arrange
        mock_df = MagicMock()
        mock_df.to_json.side_effect = Exception("File write error")
        mock_read_parquet.return_value = mock_df

        file_path = "/path/input.parquet"

        # Act
        with patch("application_sdk.common.file_converter.logger.error") as mock_logger:
            result = convert_parquet_to_json(file_path)

        # Assert
        assert result is None
        mock_logger.assert_called_once()

    @patch("pandas.read_parquet")
    def test_parquet_to_json_proper_parameters(self, mock_read_parquet):
        """Test that JSON is written with correct parameters for line-delimited format."""
        # Arrange
        mock_df = MagicMock()
        mock_read_parquet.return_value = mock_df

        file_path = "/path/input.parquet"

        # Act
        convert_parquet_to_json(file_path)

        # Assert
        mock_df.to_json.assert_called_once_with(
            "/path/input.json", orient="records", lines=True
        )


class TestRegistryIntegration:
    """Test suite for registry integration and converter lookup."""

    def test_json_to_parquet_registered_in_registry(self):
        """Test that JSON to Parquet converter is properly registered."""
        converter = file_converter_registry.registry.get(ConvertFile.JSON_TO_PARQUET)
        assert converter is not None
        assert converter == convert_json_to_parquet

    def test_parquet_to_json_registered_in_registry(self):
        """Test that Parquet to JSON converter is properly registered."""
        converter = file_converter_registry.registry.get(ConvertFile.PARQUET_TO_JSON)
        assert converter is not None
        assert converter == convert_parquet_to_json


class TestEdgeCasesAndErrorHandling:
    """Test suite for edge cases and error handling scenarios."""

    def test_convert_file_enum_construction_with_valid_types(self):
        """Test that ConvertFile enum can be constructed with valid type combinations."""
        # These should work based on defined enums
        json_to_parquet = ConvertFile("json_to_parquet")
        parquet_to_json = ConvertFile("parquet_to_json")

        assert json_to_parquet == ConvertFile.JSON_TO_PARQUET
        assert parquet_to_json == ConvertFile.PARQUET_TO_JSON

    def test_convert_file_enum_construction_with_invalid_types(self):
        """Test that ConvertFile enum raises ValueError for unsupported combinations."""
        with pytest.raises(ValueError):
            ConvertFile("csv_to_parquet")  # Unsupported conversion

        with pytest.raises(ValueError):
            ConvertFile("json_to_csv")  # Unsupported conversion

    def test_file_type_enum_values(self):
        """Test that FileType enum has expected values."""
        assert FileType.JSON.value == "json"
        assert FileType.PARQUET.value == "parquet"


class TestFileConverterIntegration:
    """Integration tests using actual files created during test setup."""

    @pytest.mark.asyncio
    async def test_convert_real_json_to_parquet(self, tmp_path):
        """Test converting an actual JSON file to Parquet format.

        Files live under ``tmp_path`` and are not unlinked here: on Windows an
        immediate ``unlink()`` of the just-read parquet file can fail with
        WinError 32 while another handle on it is still closing. pytest's own
        tmp_path cleanup tolerates that.
        """
        json_file_path = tmp_path / "input.json"
        # Line-delimited JSON, as the converter expects.
        json_file_path.write_text(
            '{"id": 1, "name": "Alice", "age": 30, "city": "New York", "active": true}\n'
            '{"id": 2, "name": "Bob", "age": 25, "city": "San Francisco", "active": false}\n'
            '{"id": 3, "name": "Charlie", "age": 35, "city": "Chicago", "active": true}\n'
        )

        result = await convert_data_files([str(json_file_path)], FileType.PARQUET)

        assert len(result) == 1
        parquet_file = Path(result[0])
        assert parquet_file == tmp_path / "input.parquet"
        assert parquet_file.exists()

        df = pd.read_parquet(parquet_file)
        assert len(df) == 3
        assert "id" in df.columns
        assert "name" in df.columns
        assert set(df["name"].values) == {"Alice", "Bob", "Charlie"}

    @pytest.mark.asyncio
    async def test_convert_real_parquet_to_json(self, tmp_path):
        """Test converting an actual Parquet file to JSON format.

        Uses ``tmp_path`` with no manual cleanup, for the same reason as
        ``test_convert_real_json_to_parquet``.
        """
        data = {
            "id": [10, 20, 30],
            "product": ["Laptop", "Mouse", "Keyboard"],
            "price": [999.99, 29.99, 79.99],
            "in_stock": [True, False, True],
        }
        parquet_file_path = tmp_path / "input.parquet"
        pd.DataFrame(data).to_parquet(parquet_file_path, index=False)

        result = await convert_data_files([str(parquet_file_path)], FileType.JSON)

        assert len(result) == 1
        json_file = Path(result[0])
        assert json_file == tmp_path / "input.json"
        assert json_file.exists()

        df_result = pd.read_json(json_file, orient="records", lines=True)
        assert len(df_result) == 3
        assert "id" in df_result.columns
        assert "product" in df_result.columns
        assert set(df_result["product"].values) == {"Laptop", "Mouse", "Keyboard"}
