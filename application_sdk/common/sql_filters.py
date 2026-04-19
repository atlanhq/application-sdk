"""SQL filter pipeline — query preparation, regex normalization, database name extraction.

This module consolidates the SQL-specific filter utilities that were previously
in ``common/utils.py``. All functions relate to preparing SQL queries with
include/exclude filter patterns.
"""

import glob
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from application_sdk.common.error_codes import CommonError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def extract_database_names_from_regex_common(
    normalized_regex: str,
    empty_default: str,
    require_wildcard_schema: bool,
) -> str:
    """Common implementation for extracting database names from regex patterns.

    Args:
        normalized_regex: The normalized regex pattern containing database.schema patterns
        empty_default: Default value to return for empty/null inputs
        require_wildcard_schema: Whether to only extract database names for wildcard schemas

    Returns:
        A regex string in the format ^(name1|name2|...)$ or default values
    """
    try:
        if not normalized_regex or normalized_regex == "^$":
            return empty_default

        if normalized_regex == ".*":
            return "'.*'"

        database_names: Set[str] = set()
        patterns = normalized_regex.split("|")

        for pattern in patterns:
            try:
                if not pattern or not pattern.strip():
                    continue

                parts = pattern.split("\\.")

                if require_wildcard_schema:
                    if len(parts) < 2:
                        logger.warning("Invalid database name format: %s", pattern)
                        continue
                    db_name = parts[0].strip()
                    schema_part = parts[1].strip()
                    if not (
                        db_name and db_name not in (".*", "^$") and schema_part == "*"
                    ):
                        continue
                else:
                    if not parts:
                        continue
                    db_name = parts[0].strip()
                    if not (db_name and db_name not in (".*", "^$")):
                        continue

                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$-]*$", db_name):
                    database_names.add(db_name)
                else:
                    logger.warning("Invalid database name format: %s", db_name)
            except Exception:
                logger.warning("Error processing pattern: %s", pattern, exc_info=True)
                continue

        if not database_names:
            return empty_default
        return f"'^({'|'.join(sorted(database_names))})$'"

    except Exception:
        logger.error(
            "Error extracting database names from regex",
            normalized_regex=normalized_regex,
            exc_info=True,
        )
        return empty_default


def transform_posix_regex(regex_pattern: str) -> str:
    r"""Transform regex pattern for POSIX compatibility.

    Rules:
    1. Add ^ before each database name before \.
    2. Add an additional . between \. and * if * follows \.

    Example: 'dev\.public$|dev\.atlan_test_schema$|wide_world_importers\.*'
    Becomes: '^dev\.public$|^dev\.atlan_test_schema$|^wide_world_importers\..*'
    """
    if not regex_pattern:
        return regex_pattern

    patterns = regex_pattern.split("|")
    transformed_patterns = []

    for pattern in patterns:
        if not pattern.startswith("^"):
            pattern = "^" + pattern
            pattern = re.sub(r"\\\.\*", r"\..*", pattern)
        transformed_patterns.append(pattern)

    return "|".join(transformed_patterns)


def prepare_query(
    query: Optional[str],
    workflow_args: Dict[str, Any],
    temp_table_regex_sql: Optional[str] = "",
    use_posix_regex: Optional[bool] = False,
) -> Optional[str]:
    """Prepare a SQL query by applying include/exclude filters.

    Modifies the provided SQL query using filters and settings defined in
    workflow_args. The include and exclude filters determine which data should
    be included or excluded from the query.

    Args:
        query: The base SQL query string to modify with filters.
        workflow_args: Dictionary containing metadata and workflow-related arguments.
        temp_table_regex_sql: SQL snippet for excluding temporary tables.
        use_posix_regex: Whether to use POSIX-compatible regex.

    Returns:
        The prepared SQL query with filters applied, or None if an error occurs.
    """
    try:
        if not query:
            logger.warning("SQL query is not set.")
            return None

        metadata = workflow_args.get("metadata", {})

        include_filter = metadata.get("include-filter") or "{}"
        exclude_filter = metadata.get("exclude-filter") or "{}"
        if metadata.get("temp-table-regex") and temp_table_regex_sql is not None:
            temp_table_regex_sql = temp_table_regex_sql.format(
                exclude_table_regex=metadata.get("temp-table-regex")
            )
        else:
            temp_table_regex_sql = ""

        normalized_include_regex, normalized_exclude_regex = prepare_filters(
            include_filter, exclude_filter
        )

        if use_posix_regex:
            normalized_include_regex_posix = transform_posix_regex(
                normalized_include_regex
            )
            normalized_exclude_regex_posix = transform_posix_regex(
                normalized_exclude_regex
            )

        include_databases = extract_database_names_from_regex_common(
            normalized_regex=normalized_include_regex,
            empty_default="'.*'",
            require_wildcard_schema=False,
        )
        exclude_databases = extract_database_names_from_regex_common(
            normalized_regex=normalized_exclude_regex,
            empty_default="'^$'",
            require_wildcard_schema=True,
        )

        exclude_empty_tables = metadata.get("exclude_empty_tables", False)
        exclude_views = metadata.get("exclude_views", False)

        if use_posix_regex:
            return query.format(
                include_databases=include_databases,
                exclude_databases=exclude_databases,
                normalized_include_regex=normalized_include_regex_posix,
                normalized_exclude_regex=normalized_exclude_regex_posix,
                temp_table_regex_sql=temp_table_regex_sql,
                exclude_empty_tables=exclude_empty_tables,
                exclude_views=exclude_views,
            )
        else:
            return query.format(
                include_databases=include_databases,
                exclude_databases=exclude_databases,
                normalized_include_regex=normalized_include_regex,
                normalized_exclude_regex=normalized_exclude_regex,
                temp_table_regex_sql=temp_table_regex_sql,
                exclude_empty_tables=exclude_empty_tables,
                exclude_views=exclude_views,
            )
    except CommonError as e:
        error_message = str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
        logger.error(
            "Error preparing query",
            query=query,
            error_message=error_message,
            error_code=CommonError.QUERY_PREPARATION_ERROR.code,
        )
        return None


async def get_database_names(
    sql_client, workflow_args, fetch_database_sql
) -> Optional[List[str]]:
    """Get database names from include-filter or by running a SQL query.

    Args:
        sql_client: SQL client for executing queries.
        workflow_args: The workflow arguments.
        fetch_database_sql: SQL query to fetch all database names.

    Returns:
        List of database names.
    """
    database_names = parse_filter_input(
        workflow_args.get("metadata", {}).get("include-filter", {})
    )

    database_names = [
        re.sub(r"^[^\w]+|[^\w]+$", "", database_name)
        for database_name in database_names
    ]
    if not database_names:
        temp_table_regex_sql = workflow_args.get("metadata", {}).get(
            "temp-table-regex", ""
        )
        prepared_query = prepare_query(
            query=fetch_database_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=temp_table_regex_sql,
            use_posix_regex=True,
        )
        database_dataframe = await sql_client.get_results(prepared_query)
        database_names = list(database_dataframe["database_name"])
    return database_names


def parse_filter_input(
    filter_input: Union[str, Dict[str, Any], None],
) -> Dict[str, Any]:
    """Robustly parse filter input from various formats.

    Args:
        filter_input: Can be None, empty string, JSON string, or dict.

    Returns:
        Parsed filter dictionary (empty dict if input is invalid/empty).
    """
    if not filter_input:
        return {}
    if isinstance(filter_input, dict):
        return filter_input
    if isinstance(filter_input, str):
        if not filter_input.strip():
            return {}
        try:
            return json.loads(filter_input)
        except json.JSONDecodeError as e:
            raise CommonError(f"Invalid filter JSON: {str(e)}") from e


def prepare_filters(
    include_filter_str: str, exclude_filter_str: str
) -> Tuple[str, str]:
    """Prepare include/exclude filters for SQL queries.

    Args:
        include_filter_str: The include filter string.
        exclude_filter_str: The exclude filter string.

    Returns:
        Tuple of (normalized_include_regex, normalized_exclude_regex).

    Raises:
        CommonError: If JSON parsing fails for either filter.
    """
    include_filter = parse_filter_input(include_filter_str)
    exclude_filter = parse_filter_input(exclude_filter_str)

    normalized_include_filter_list = normalize_filters(include_filter, True)
    normalized_exclude_filter_list = normalize_filters(exclude_filter, False)

    normalized_include_regex = (
        "|".join(normalized_include_filter_list)
        if normalized_include_filter_list
        else ".*"
    )
    normalized_exclude_regex = (
        "|".join(normalized_exclude_filter_list)
        if normalized_exclude_filter_list
        else "^$"
    )

    return normalized_include_regex, normalized_exclude_regex


def normalize_filters(
    filter_dict: Dict[str, List[str] | str], is_include: bool
) -> List[str]:
    """Normalize filter dict to regex patterns.

    Args:
        filter_dict: The filter dictionary.
        is_include: Whether the filter is an include filter.

    Returns:
        List of normalized filter patterns.
    """
    normalized_filter_list: List[str] = []
    for filtered_db, filtered_schemas in filter_dict.items():
        db = filtered_db.strip("^$")

        if filtered_schemas == "*":
            normalized_filter_list.append(f"{db}\\.*")
            continue

        if not filtered_schemas:
            normalized_filter_list.append(f"{db}\\.*")
            continue

        if isinstance(filtered_schemas, list):
            for schema in filtered_schemas:
                sch = schema.lstrip("^")
                normalized_filter_list.append(f"{db}\\.{sch}")

    return normalized_filter_list


def read_sql_files(
    queries_prefix: str = f"{os.path.dirname(os.path.abspath(__file__))}/queries",
) -> Dict[str, str]:
    """Read all SQL files from a directory and return as a name→content mapping.

    Args:
        queries_prefix: Directory containing SQL query files.

    Returns:
        Dictionary mapping SQL file names (uppercase, without extension) to contents.
    """
    sql_files: List[str] = glob.glob(
        os.path.join(queries_prefix, "**/*.sql"),
        recursive=True,
    )

    result: Dict[str, str] = {}
    for file in sql_files:
        with open(file, encoding="utf-8") as f:
            result[os.path.basename(file).upper().replace(".SQL", "")] = (
                f.read().strip()
            )

    return result
