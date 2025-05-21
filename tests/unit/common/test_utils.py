import os
from pathlib import Path
from typing import Dict, List, Union
from unittest.mock import Mock, mock_open, patch

from application_sdk.common.error_codes import CommonError
from application_sdk.common.utils import (
    get_workflow_config,
    normalize_filters,
    prepare_filters,
    prepare_query,
    read_sql_files,
    update_workflow_config,
)


class TestPrepareQuery:
    def test_successful_query_preparation(self) -> None:
        """Test successful query preparation with all parameters"""
        query = "SELECT * FROM {normalized_include_regex} WHERE {normalized_exclude_regex} {temp_table_regex_sql}"
        workflow_args: Dict[str, Dict[str, Union[str, bool]]] = {
            "metadata": {
                "include-filter": '{"db1": ["schema1"]}',
                "exclude-filter": '{"db2": ["schema2"]}',
                "temp-table-regex": "temp.*",
                "exclude_empty_tables": True,
                "exclude_views": True,
            }
        }
        temp_table_regex_sql = "AND table_name NOT LIKE '{exclude_table_regex}'"

        result = prepare_query(query, workflow_args, temp_table_regex_sql)

        assert result is not None
        assert "db1\\.schema1" in result
        assert "db2\\.schema2" in result
        assert "temp.*" in result

    def test_query_preparation_without_filters(self) -> None:
        """Test query preparation without any filters"""
        query = "SELECT * FROM {normalized_include_regex}"
        workflow_args: Dict[str, Dict[str, str]] = {"metadata": {}}

        result = prepare_query(query, workflow_args)

        assert result is not None
        assert ".*" in result  # Default include regex when no filters provided

    def test_query_preparation_with_empty_filters(self) -> None:
        """Test query preparation with empty filter strings"""
        query = (
            "SELECT * FROM {normalized_include_regex} WHERE {normalized_exclude_regex}"
        )
        workflow_args: Dict[str, Dict[str, str]] = {
            "metadata": {
                "include-filter": "",
                "exclude-filter": "",
            }
        }

        result = prepare_query(query, workflow_args)

        assert result is not None
        assert ".*" in result  # Default include regex
        assert "^$" in result  # Default exclude regex

    def test_query_preparation_with_invalid_json(self) -> None:
        """Test query preparation with invalid JSON in filters"""
        query = "SELECT * FROM {normalized_include_regex}"
        workflow_args: Dict[str, Dict[str, str]] = {
            "metadata": {
                "include-filter": "invalid json",
            }
        }

        with patch("application_sdk.common.utils.logger") as mock_logger:
            result = prepare_query(query, workflow_args)
            mock_logger.error.assert_called_once_with(
                "Error preparing query [SELECT * FROM {normalized_include_regex}]:  Expecting value: line 1 column 1 (char 0)",
                error_code=CommonError.QUERY_PREPARATION_ERROR.code,
            )
            assert result is None

    def test_query_preparation_with_missing_metadata(self) -> None:
        """Test query preparation with missing metadata"""
        query = "SELECT * FROM {normalized_include_regex}"
        workflow_args: Dict[str, Dict[str, str]] = {}

        result = prepare_query(query, workflow_args)

        assert result is not None
        assert ".*" in result  # Should use default include regex


class TestPrepareFilters:
    def test_prepare_filters_with_valid_input(self) -> None:
        """Test prepare_filters with valid include and exclude filters"""
        include_filter = '{"db1": ["schema1", "schema2"], "db2": ["schema3"]}'
        exclude_filter = '{"db3": ["schema4"]}'

        include_regex, exclude_regex = prepare_filters(include_filter, exclude_filter)

        assert "db1\\.schema1|db1\\.schema2|db2\\.schema3" == include_regex
        assert "db3\\.schema4" == exclude_regex

    def test_prepare_filters_with_empty_filters(self) -> None:
        """Test prepare_filters with empty filters"""
        include_filter = "{}"
        exclude_filter = "{}"

        include_regex, exclude_regex = prepare_filters(include_filter, exclude_filter)

        assert ".*" == include_regex
        assert "^$" == exclude_regex

    def test_prepare_filters_with_wildcard(self) -> None:
        """Test prepare_filters with wildcard schema"""
        include_filter = '{"db1": "*"}'
        exclude_filter = "{}"

        include_regex, exclude_regex = prepare_filters(include_filter, exclude_filter)

        assert "db1\\.*" == include_regex
        assert "^$" == exclude_regex

    def test_prepare_filters_with_empty_include_and_filled_exclude(self) -> None:
        """Test prepare_filters with empty include filter but filled exclude filter"""
        include_filter = "{}"
        exclude_filter = '{"db1": ["schema1"], "db2": ["schema2"]}'

        include_regex, exclude_regex = prepare_filters(include_filter, exclude_filter)

        assert ".*" == include_regex
        assert "db1\\.schema1|db2\\.schema2" == exclude_regex


class TestNormalizeFilters:
    def test_normalize_filters_with_specific_schemas(self) -> None:
        """Test normalize_filters with specific schema list"""
        filter_dict: Dict[str, Union[List[str], str]] = {
            "db1": ["schema1", "schema2"],
            "db2": ["schema3"],
        }
        result = normalize_filters(filter_dict, True)

        assert sorted(result) == sorted(
            ["db1\\.schema1", "db1\\.schema2", "db2\\.schema3"]
        )

    def test_normalize_filters_with_wildcard(self) -> None:
        """Test normalize_filters with wildcard schema"""
        filter_dict: Dict[str, Union[List[str], str]] = {"db1": "*"}
        result = normalize_filters(filter_dict, True)

        assert result == ["db1\\.*"]

    def test_normalize_filters_with_empty_list(self) -> None:
        """Test normalize_filters with empty schema list"""
        filter_dict: Dict[str, Union[List[str], str]] = {"db1": []}
        result = normalize_filters(filter_dict, True)

        assert result == ["db1\\.*"]

    def test_normalize_filters_with_regex_patterns(self) -> None:
        """Test normalize_filters with regex patterns in database names"""
        filter_dict: Dict[str, Union[List[str], str]] = {"^db1$": ["^schema1$"]}
        result = normalize_filters(filter_dict, True)

        # The implementation strips ^ from schema names but keeps $
        assert result == ["db1\\.schema1$"]


class TestWorkflowConfig:
    @patch("application_sdk.common.utils.StateStoreInput.get_state")
    def test_get_workflow_config(self, mock_get_state: Mock) -> None:
        """Test getting workflow configuration"""
        expected_config = {"key": "value"}
        mock_get_state.return_value = expected_config

        # Call the function
        result = get_workflow_config("test_config_id")

        # Assertions
        assert result == expected_config
        mock_get_state.assert_called_once_with("config_test_config_id")

    @patch("application_sdk.common.utils.StateStoreInput.extract_configuration")
    @patch("application_sdk.common.utils.StateStoreOutput.store_configuration")
    def test_update_workflow_config(self, mock_store: Mock, mock_extract: Mock) -> None:
        """Test updating workflow configuration"""
        existing_config = {"key1": "value1", "key2": "value2"}
        update_config = {"key1": "new_value", "key3": "value3"}
        mock_extract.return_value = existing_config

        result = update_workflow_config("test_config_id", update_config)

        expected_config = {"key1": "new_value", "key2": "value2"}
        assert result == expected_config
        mock_store.assert_called_once_with("test_config_id", expected_config)

    @patch("application_sdk.common.utils.StateStoreInput.extract_configuration")
    @patch("application_sdk.common.utils.StateStoreOutput.store_configuration")
    def test_update_workflow_config_with_none_values(
        self, mock_store: Mock, mock_extract: Mock
    ) -> None:
        """Test updating workflow configuration with None values"""
        existing_config = {"key1": "value1", "key2": "value2"}
        update_config = {"key1": None, "key2": "new_value"}
        mock_extract.return_value = existing_config

        result = update_workflow_config("test_config_id", update_config)

        expected_config = {"key1": "value1", "key2": "new_value"}
        assert result == expected_config
        mock_store.assert_called_once_with("test_config_id", expected_config)


def test_read_sql_files_with_multiple_files(tmp_path: Path):
    """Test read_sql_files with multiple SQL files in different directories."""
    mock_files = {
        os.path.join("queries", "extraction", "table.sql"): "SELECT * FROM tables;",
        os.path.join("queries", "schema.sql"): "SELECT * FROM schemas;",
        os.path.join("queries", "views.sql"): "SELECT * FROM views;",
    }

    expected_result = {
        "TABLE": "SELECT * FROM tables;",
        "SCHEMA": "SELECT * FROM schemas;",
        "VIEWS": "SELECT * FROM views;",
    }

    with patch("glob.glob") as mock_glob, patch(
        "builtins.open", new_callable=mock_open
    ) as mock_file_open, patch("os.path.dirname", return_value="/mock/path"):
        # Configure glob to return our mock files
        mock_glob.return_value = [
            os.path.join("/mock/path", file_path) for file_path in mock_files.keys()
        ]

        # Configure file open to return different content for different files
        mock_file = mock_file_open.return_value
        mock_file.read.side_effect = list(mock_files.values())

        result = read_sql_files("/mock/path")

        # Verify the results
        assert result == expected_result

        # Verify glob was called correctly
        mock_glob.assert_called_once_with(
            os.path.join("/mock/path", "**/*.sql"), recursive=True
        )

        # Verify files were opened
        assert mock_file_open.call_count == len(mock_files)


def test_read_sql_files_with_empty_directory():
    """Test read_sql_files when no SQL files are found."""
    with patch("glob.glob", return_value=[]), patch(
        "os.path.dirname", return_value="/mock/path"
    ):
        result = read_sql_files("/mock/path")
        assert result == {}


def test_read_sql_files_with_whitespace():
    """Test read_sql_files handles whitespace in SQL content correctly."""
    sql_content = """
    SELECT *
    FROM tables
    WHERE id > 0;
    """

    expected_content = "SELECT *\n    FROM tables\n    WHERE id > 0;"

    with patch("glob.glob") as mock_glob, patch(
        "builtins.open", mock_open(read_data=sql_content)
    ), patch("os.path.dirname", return_value="/mock/path"):
        mock_glob.return_value = ["/mock/path/queries/test.sql"]

        result = read_sql_files("/mock/path")
        assert result == {"TEST": expected_content.strip()}


def test_read_sql_files_case_sensitivity():
    """Test read_sql_files handles file names with different cases correctly."""
    mock_files = {
        os.path.join("queries", "UPPER.SQL"): "upper case",
        os.path.join("queries", "lower.sql"): "lower case",
        os.path.join("queries", "Mixed.Sql"): "mixed case",
    }

    expected_result = {
        "UPPER": "upper case",
        "LOWER": "lower case",
        "MIXED": "mixed case",
    }

    with patch("glob.glob") as mock_glob, patch(
        "builtins.open", new_callable=mock_open
    ) as mock_file_open, patch("os.path.dirname", return_value="/mock/path"):
        mock_glob.return_value = [
            os.path.join("/mock/path", file_path) for file_path in mock_files.keys()
        ]

        mock_file = mock_file_open.return_value
        mock_file.read.side_effect = list(mock_files.values())

        result = read_sql_files("/mock/path")
        assert result == expected_result
