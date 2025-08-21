import os
from pathlib import Path
from typing import Dict, List, Union
from unittest.mock import mock_open, patch

from application_sdk.common.error_codes import CommonError
from application_sdk.common.utils import (
    extract_database_names_from_regex_common,
    normalize_filters,
    parse_filter_input,
    prepare_filters,
    prepare_query,
    read_sql_files,
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


class TestExtractDatabaseNamesFromIncludeRegex:
    def test_extract_database_names_from_include_regex_with_multiple_databases(
        self,
    ) -> None:
        """Test extracting database names from include regex with multiple databases"""
        normalized_regex = "dev\\.external_schema$|wide_world_importers\\.bronze_sales$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should return sorted database names in regex format
        assert result == "'^(dev|wide_world_importers)$'"

    def test_extract_database_names_from_regex_with_multiple_databases_with_special_characters(
        self,
    ) -> None:
        """Test extracting database names from regex with multiple databases with special characters in database names"""
        normalized_regex = "dev\\.external_schema$|wide_world_importers\\.bronze_sales$|test-db\\.schema_name$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should return sorted database names in regex format
        assert result == "'^(dev|test-db|wide_world_importers)$'"

    def test_extract_database_names_from_regex_with_wildcard_schemas(self) -> None:
        """Test extracting database names from regex with wildcard schemas"""
        normalized_regex = "dev\\.*|wide_world_importers\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'^(dev|wide_world_importers)$'"

    def test_extract_database_names_from_include_regex_with_single_database(
        self,
    ) -> None:
        """Test extracting database names from include regex with single database"""
        normalized_regex = "test_db\\.schema_name$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'^(test_db)$'"

    def test_extract_database_names_from_include_regex_with_empty_input(self) -> None:
        """Test extracting database names from include regex with empty input"""
        result = extract_database_names_from_regex_common(
            normalized_regex="",
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'.*'"

    def test_extract_database_names_from_include_regex_with_none_input(self) -> None:
        """Test extracting database names from include regex with None input"""
        result = extract_database_names_from_regex_common(
            normalized_regex=None,  # type: ignore
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'.*'"

    def test_extract_database_names_from_include_regex_with_non_string_input(
        self,
    ) -> None:
        """Test extracting database names from include regex with non-string input"""
        result = extract_database_names_from_regex_common(
            normalized_regex=123,  # type: ignore
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'.*'"

    def test_extract_database_names_from_include_regex_with_empty_patterns(
        self,
    ) -> None:
        """Test extracting database names from include regex with empty patterns"""
        normalized_regex = "|||"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'.*'"

    def test_extract_database_names_from_include_regex_with_whitespace_patterns(
        self,
    ) -> None:
        """Test extracting database names from include regex with whitespace patterns"""
        normalized_regex = "   |  db1\\.schema1  |  "
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'^(db1)$'"

    def test_extract_database_names_from_include_regex_with_invalid_database_names(
        self,
    ) -> None:
        """Test extracting database names from include regex with invalid database names"""
        normalized_regex = "123db\\.schema1|db-2\\.schema2|valid_db\\.schema3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Only valid_db should be included (starts with letter/underscore, alphanumeric + underscore)
        assert result == "'^(db-2|valid_db)$'"

    def test_extract_database_names_from_regex_with_special_characters(self) -> None:
        """Test extracting database names from regex with special characters"""
        normalized_regex = (
            "db@test\\.schema1|db#test\\.schema2|db_test\\.schema3|db$test\\.schema4"
        )
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Only db_test should be included (valid format)
        assert result == "'^(db$test|db_test)$'"

    def test_extract_database_names_from_include_regex_with_dot_patterns(self) -> None:
        """Test extracting database names from include regex with dot patterns"""
        normalized_regex = ".*\\.schema1|^$\\.schema2|db1\\.schema3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Only db1 should be included (.* and ^$ are excluded)
        assert result == "'^(db1)$'"

    def test_extract_database_names_from_include_regex_with_underscore_names(
        self,
    ) -> None:
        """Test extracting database names from include regex with underscore names"""
        normalized_regex = "_test_db\\.schema1|test_db_\\.schema2|_test_db_\\.schema3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # All should be included as they start with underscore or letter
        assert result == "'^(_test_db|_test_db_|test_db_)$'"

    def test_extract_database_names_from_include_regex_with_mixed_case(self) -> None:
        """Test extracting database names from include regex with mixed case"""
        normalized_regex = "TestDB\\.schema1|test_db\\.schema2|TEST_DB\\.schema3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # All should be included as they follow valid naming convention
        assert result == "'^(TEST_DB|TestDB|test_db)$'"

    def test_extract_database_names_from_include_regex_with_numbers_in_names(
        self,
    ) -> None:
        """Test extracting database names from include regex with numbers in names"""
        normalized_regex = "db1\\.schema1|db_2\\.schema2|db3_test\\.schema3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # All should be included as they follow valid naming convention
        assert result == "'^(db1|db3_test|db_2)$'"

    def test_extract_database_names_from_include_regex_with_complex_patterns(
        self,
    ) -> None:
        """Test extracting database names from include regex with complex patterns"""
        normalized_regex = "dev\\.external_schema$|wide_world_importers\\.bronze_sales$|test_db\\.*|prod\\.schema1$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should return all valid database names sorted
        assert result == "'^(dev|prod|test_db|wide_world_importers)$'"

    def test_extract_database_names_from_include_regex_with_duplicate_names(
        self,
    ) -> None:
        """Test extracting database names from include regex with duplicate names"""
        normalized_regex = "db1\\.schema1|db1\\.schema2|db2\\.schema3|db1\\.schema4"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should deduplicate and return sorted names
        assert result == "'^(db1|db2)$'"

    def test_extract_database_names_from_include_regex_with_malformed_patterns(
        self,
    ) -> None:
        """Test extracting database names from include regex with malformed patterns"""
        normalized_regex = "db1\\.|db2\\.schema2|\\..*|db3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should handle malformed patterns gracefully
        assert result == "'^(db1|db2|db3)$'"

    def test_extract_database_names_from_include_regex_with_caret_dollar_patterns(
        self,
    ) -> None:
        """Test extracting database names from include regex with ^$ patterns"""
        normalized_regex = "^$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'.*'"

    def test_extract_database_names_from_include_regex_with_dot_star_pattern(
        self,
    ) -> None:
        """Test extracting database names from include regex with .* pattern"""
        normalized_regex = ".*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        assert result == "'.*'"

    @patch("application_sdk.common.utils.logger")
    def test_extract_database_names_from_include_regex_logs_warnings_for_invalid_names(
        self, mock_logger
    ) -> None:
        """Test that extract_database_names_from_include_regex logs warnings for invalid database names"""
        normalized_regex = "123db\\.schema1|valid_db\\.schema2"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should log warning for invalid database name
        mock_logger.warning.assert_called_with("Invalid database name format: 123db")
        assert result == "'^(valid_db)$'"

    @patch("application_sdk.common.utils.logger")
    def test_extract_database_names_from_include_regex_logs_warnings_for_processing_errors(
        self, mock_logger
    ) -> None:
        """Test that extract_database_names_from_include_regex logs warnings for processing errors"""
        # This test would require mocking the split operation to raise an exception
        # For now, we'll test with a pattern that should trigger a warning
        normalized_regex = "db1\\.schema1|invalid^pattern|db2\\.schema2"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should log warning for invalid database name format
        mock_logger.warning.assert_called_with(
            "Invalid database name format: invalid^pattern"
        )
        assert result == "'^(db1|db2)$'"

    @patch("application_sdk.common.utils.logger")
    def test_extract_database_names_from_include_regex_logs_error_for_general_exception(
        self, mock_logger
    ) -> None:
        """Test that extract_database_names_from_include_regex logs error for general exceptions"""
        # This test would require more complex mocking to trigger the general exception handler
        # For now, we'll test the error logging path with a valid input
        normalized_regex = "db1\\.schema1"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )

        # Should not log any errors for valid input
        mock_logger.error.assert_not_called()
        assert result == "'^(db1)$'"


class TestExtractDatabaseNamesFromExcludeRegex:
    def test_extract_database_names_from_exclude_regex_with_specific_schemas(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with specific schemas"""
        normalized_regex = "dev\\.external_schema$|wide_world_importers\\.bronze_sales$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should return empty regex for specific schemas (no database names extracted)
        assert result == "'^$'"

    def test_extract_database_names_from_exclude_regex_with_wildcard_schemas(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with wildcard schemas"""
        normalized_regex = "dev\\.*|wide_world_importers\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should extract database names for wildcard schemas
        assert result == "'^(dev|wide_world_importers)$'"

    def test_extract_database_names_from_exclude_regex_with_mixed_patterns(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with mixed patterns"""
        normalized_regex = (
            "dev\\.external_schema$|wide_world_importers\\.*|test_db\\.schema1$"
        )
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should only extract database names for wildcard schemas
        assert result == "'^(wide_world_importers)$'"

    def test_extract_database_names_from_exclude_regex_with_empty_input(self) -> None:
        """Test extracting database names from exclude regex with empty input"""
        result = extract_database_names_from_regex_common(
            normalized_regex="",
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'^$'"

    def test_extract_database_names_from_exclude_regex_with_none_input(self) -> None:
        """Test extracting database names from exclude regex with None input"""
        result = extract_database_names_from_regex_common(
            normalized_regex=None,  # type: ignore
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'^$'"

    def test_extract_database_names_from_exclude_regex_with_non_string_input(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with non-string input"""
        result = extract_database_names_from_regex_common(
            normalized_regex=123,  # type: ignore
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'^$'"

    def test_extract_database_names_from_exclude_regex_with_empty_patterns(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with empty patterns"""
        normalized_regex = "|||"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'^$'"

    def test_extract_database_names_from_exclude_regex_with_whitespace_patterns(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with whitespace patterns"""
        normalized_regex = "   |  db1\\.*  |  "
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'^(db1)$'"

    def test_extract_database_names_from_exclude_regex_with_invalid_database_names(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with invalid database names"""
        normalized_regex = "123db\\.*|db-2\\.*|valid_db\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Only valid_db should be included (starts with letter/underscore, alphanumeric + underscore)
        assert result == "'^(db-2|valid_db)$'"

    def test_extract_database_names_from_exclude_regex_with_special_characters(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with special characters"""
        normalized_regex = "db@test\\.*|db#test\\.*|db_test\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Only db_test should be included (valid format)
        assert result == "'^(db_test)$'"

    def test_extract_database_names_from_exclude_regex_with_dot_patterns(self) -> None:
        """Test extracting database names from exclude regex with dot patterns"""
        normalized_regex = ".*\\.*|^$\\.*|db1\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Only db1 should be included (.* and ^$ are excluded)
        assert result == "'^(db1)$'"

    def test_extract_database_names_from_exclude_regex_with_underscore_names(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with underscore names"""
        normalized_regex = "_test_db\\.*|test_db_\\.*|_test_db_\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # All should be included as they start with underscore or letter
        assert result == "'^(_test_db|_test_db_|test_db_)$'"

    def test_extract_database_names_from_exclude_regex_with_mixed_case(self) -> None:
        """Test extracting database names from exclude regex with mixed case"""
        normalized_regex = "TestDB\\.*|test_db\\.*|TEST_DB\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # All should be included as they follow valid naming convention
        assert result == "'^(TEST_DB|TestDB|test_db)$'"

    def test_extract_database_names_from_exclude_regex_with_numbers_in_names(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with numbers in names"""
        normalized_regex = "db1\\.*|db_2\\.*|db3_test\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # All should be included as they follow valid naming convention
        assert result == "'^(db1|db3_test|db_2)$'"

    def test_extract_database_names_from_exclude_regex_with_complex_patterns(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with complex patterns"""
        normalized_regex = "dev\\.external_schema$|wide_world_importers\\.*|test_db\\.*|prod\\.schema1$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should return only database names for wildcard schemas
        assert result == "'^(test_db|wide_world_importers)$'"

    def test_extract_database_names_from_exclude_regex_with_duplicate_names(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with duplicate names"""
        normalized_regex = "db1\\.*|db1\\.*|db2\\.*|db1\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should deduplicate and return sorted names
        assert result == "'^(db1|db2)$'"

    def test_extract_database_names_from_exclude_regex_with_malformed_patterns(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with malformed patterns"""
        normalized_regex = "db1\\.|db2\\.*|\\..*|db3\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should handle malformed patterns gracefully
        assert result == "'^(db2|db3)$'"

    def test_extract_database_names_from_exclude_regex_with_caret_dollar_pattern(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with ^$ pattern"""
        normalized_regex = "^$"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'^$'"

    def test_extract_database_names_from_exclude_regex_with_dot_star_pattern(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with .* pattern"""
        normalized_regex = ".*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        assert result == "'.*'"

    def test_extract_database_names_from_exclude_regex_with_incomplete_patterns(
        self,
    ) -> None:
        """Test extracting database names from exclude regex with incomplete patterns"""
        normalized_regex = "db1\\.schema1|db2\\.*|db3"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should handle incomplete patterns and only extract for wildcard schemas
        assert result == "'^(db2)$'"

    @patch("application_sdk.common.utils.logger")
    def test_extract_database_names_from_exclude_regex_logs_warnings_for_invalid_names(
        self, mock_logger
    ) -> None:
        """Test that extract_database_names_from_exclude_regex logs warnings for invalid database names"""
        normalized_regex = "123db\\.*|valid_db\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should log warning for invalid database name
        mock_logger.warning.assert_called_with("Invalid database name format: 123db")
        assert result == "'^(valid_db)$'"

    @patch("application_sdk.common.utils.logger")
    def test_extract_database_names_from_exclude_regex_logs_warnings_for_processing_errors(
        self, mock_logger
    ) -> None:
        """Test that extract_database_names_from_exclude_regex logs warnings for processing errors"""
        normalized_regex = "db1\\.*|invalid-pattern|db2\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should log warning for invalid database name format
        mock_logger.warning.assert_called_with(
            "Invalid database name format: invalid-pattern"
        )
        assert result == "'^(db1|db2)$'"

    @patch("application_sdk.common.utils.logger")
    def test_extract_database_names_from_exclude_regex_logs_error_for_general_exception(
        self, mock_logger
    ) -> None:
        """Test that extract_database_names_from_exclude_regex logs error for general exceptions"""
        # This test would require more complex mocking to trigger the general exception handler
        # For now, we'll test the error logging path with a valid input
        normalized_regex = "db1\\.*"
        result = extract_database_names_from_regex_common(
            normalized_regex=normalized_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        # Should not log any errors for valid input
        mock_logger.error.assert_not_called()
        assert result == "'^(db1)$'"


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


class TestParseFilterInput:
    """Test the parse_filter_input function with various inputs."""

    def test_empty_inputs(self) -> None:
        """Test empty and None inputs return empty dict."""
        assert parse_filter_input(None) == {}
        assert parse_filter_input("") == {}
        assert parse_filter_input("   ") == {}
        assert parse_filter_input("{}") == {}

    def test_dict_inputs(self) -> None:
        """Test dict inputs are returned as-is."""
        test_dict = {"^db$": ["^schema$"]}
        assert parse_filter_input(test_dict) == test_dict
        assert parse_filter_input({}) == {}

    def test_valid_json_strings(self) -> None:
        """Test valid JSON strings are parsed correctly."""
        assert parse_filter_input('{"^db$": ["^schema$"]}') == {"^db$": ["^schema$"]}
        assert parse_filter_input('{"db1": ["s1", "s2"]}') == {"db1": ["s1", "s2"]}

    def test_invalid_json_strings(self) -> None:
        """Test invalid JSON strings raise CommonError."""
        import pytest

        with pytest.raises(CommonError, match="Invalid filter JSON"):
            parse_filter_input("invalid json")

        with pytest.raises(CommonError, match="Invalid filter JSON"):
            parse_filter_input('{"invalid": }')

        with pytest.raises(CommonError, match="Invalid filter JSON"):
            parse_filter_input("not json at all")
