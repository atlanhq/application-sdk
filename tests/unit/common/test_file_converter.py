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

from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import pytest

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
        """Test that when converter function is None, AttributeError is raised."""
        # Arrange - registry returns None for unknown converter
        mock_registry.registry.get.return_value = None

        input_files = ["/path/input.json"]

        # Act & Assert - This should raise AttributeError when trying to call None
        with pytest.raises(TypeError):  # calling None() raises TypeError
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

    @patch("application_sdk.common.file_converter.pd.read_json")
    def test_successful_json_to_parquet_conversion(self, mock_read_json):
        """Test successful conversion from JSON to Parquet."""
        # Arrange
        mock_df = MagicMock()
        mock_filtered_df = MagicMock()

        # Mock the column filtering chain: df.loc[:, ~df.where(df.astype(bool)).isna().all(axis=0)]
        mock_df.where.return_value = mock_df
        mock_df.astype.return_value = mock_df
        mock_df.isna.return_value = mock_df
        mock_df.all.return_value = MagicMock()
        mock_df.loc.__getitem__.return_value = mock_filtered_df

        mock_read_json.return_value = mock_df

        file_path = "/path/input.json"
        expected_output = "/path/input.parquet"

        # Act
        result = convert_json_to_parquet(file_path)

        # Assert
        assert result == expected_output
        mock_read_json.assert_called_once_with(file_path, orient="records", lines=True)
        mock_filtered_df.to_parquet.assert_called_once_with(expected_output)

    @patch("application_sdk.common.file_converter.pd.read_json")
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

    @patch("application_sdk.common.file_converter.pd.read_json")
    def test_json_to_parquet_write_error_returns_none(self, mock_read_json):
        """Test that write errors return None and are logged."""
        # Arrange
        mock_df = MagicMock()
        mock_filtered_df = MagicMock()

        # Mock the column filtering chain
        mock_df.where.return_value = mock_df
        mock_df.astype.return_value = mock_df
        mock_df.isna.return_value = mock_df
        mock_df.all.return_value = MagicMock()
        mock_df.loc.__getitem__.return_value = mock_filtered_df

        # Make to_parquet raise an exception
        mock_filtered_df.to_parquet.side_effect = Exception("File write error")

        mock_read_json.return_value = mock_df

        file_path = "/path/input.json"

        # Act
        with patch("application_sdk.common.file_converter.logger.error") as mock_logger:
            result = convert_json_to_parquet(file_path)

        # Assert
        assert result is None
        mock_logger.assert_called_once()

    @patch("application_sdk.common.file_converter.pd.read_json")
    def test_json_to_parquet_column_filtering(self, mock_read_json):
        """Test that empty columns are filtered out during conversion."""
        # Arrange
        mock_df = MagicMock()
        mock_filtered_df = MagicMock()

        # Mock the column filtering chain
        mock_df.where.return_value = mock_df
        mock_df.astype.return_value = mock_df
        mock_df.isna.return_value = mock_df
        mock_df.all.return_value = MagicMock()
        mock_df.loc.__getitem__.return_value = mock_filtered_df

        mock_read_json.return_value = mock_df

        file_path = "/path/input.json"

        # Act
        convert_json_to_parquet(file_path)

        # Assert
        # Verify that column filtering logic was applied
        mock_df.where.assert_called_once()
        mock_df.astype.assert_called_once_with(bool)
        mock_df.isna.assert_called_once()
        mock_df.all.assert_called_once_with(axis=0)
        mock_filtered_df.to_parquet.assert_called_once()


class TestConvertParquetToJson:
    """Test suite for the convert_parquet_to_json function."""

    @patch("application_sdk.common.file_converter.pd.read_parquet")
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

    @patch("application_sdk.common.file_converter.pd.read_parquet")
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

    @patch("application_sdk.common.file_converter.pd.read_parquet")
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

    @patch("application_sdk.common.file_converter.pd.read_parquet")
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
    async def test_convert_real_json_to_parquet(self):
        """Test converting an actual JSON file to Parquet format."""
        import tempfile
        from pathlib import Path

        # Arrange - Create a temporary JSON file with test data
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            # Write line-delimited JSON as expected by the converter
            f.write(
                '{"id": 1, "name": "Alice", "age": 30, "city": "New York", "active": true}\n'
            )
            f.write(
                '{"id": 2, "name": "Bob", "age": 25, "city": "San Francisco", "active": false}\n'
            )
            f.write(
                '{"id": 3, "name": "Charlie", "age": 35, "city": "Chicago", "active": true}\n'
            )
            json_file_path = f.name

        try:
            # Act - Convert JSON to Parquet
            result = await convert_data_files([json_file_path], FileType.PARQUET)

            # Assert - Check conversion succeeded
            assert len(result) == 1
            parquet_file = Path(result[0])
            assert parquet_file.exists()
            assert parquet_file.suffix == ".parquet"

            # Verify parquet file has data by reading it back
            df = pd.read_parquet(parquet_file)
            assert len(df) == 3  # Should have 3 rows from JSON
            assert "id" in df.columns
            assert "name" in df.columns
            expected_names = {"Alice", "Bob", "Charlie"}
            actual_names = set(df["name"].values)
            assert actual_names == expected_names

            # Cleanup converted file
            parquet_file.unlink()

        finally:
            # Cleanup original file
            Path(json_file_path).unlink()

    @pytest.mark.asyncio
    async def test_convert_real_parquet_to_json(self):
        """Test converting an actual Parquet file to JSON format."""
        import tempfile
        from pathlib import Path

        # Arrange - Create a temporary Parquet file with test data
        data = {
            "id": [10, 20, 30],
            "product": ["Laptop", "Mouse", "Keyboard"],
            "price": [999.99, 29.99, 79.99],
            "in_stock": [True, False, True],
        }
        df = pd.DataFrame(data)

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            parquet_file_path = f.name

        # Save DataFrame to the temporary parquet file
        df.to_parquet(parquet_file_path, index=False)

        try:
            # Act - Convert Parquet to JSON
            result = await convert_data_files([parquet_file_path], FileType.JSON)

            # Assert - Check conversion succeeded
            assert len(result) == 1
            json_file = Path(result[0])
            assert json_file.exists()
            assert json_file.suffix == ".json"

            # Verify JSON file has data by reading it back
            df_result = pd.read_json(json_file, orient="records", lines=True)
            assert len(df_result) == 3  # Should have 3 rows from Parquet
            assert "id" in df_result.columns
            assert "product" in df_result.columns
            expected_products = {"Laptop", "Mouse", "Keyboard"}
            actual_products = set(df_result["product"].values)
            assert actual_products == expected_products

            # Cleanup converted file
            json_file.unlink()

        finally:
            # Cleanup original file
            Path(parquet_file_path).unlink()
