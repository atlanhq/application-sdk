import asyncio
import glob
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from application_sdk.common.error_codes import CommonError
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def extract_database_names_from_regex_common(
    normalized_regex: str,
    empty_default: str,
    require_wildcard_schema: bool,
) -> str:
    """
    Common implementation for extracting database names from regex patterns.

    Args:
        normalized_regex (str): The normalized regex pattern containing database.schema patterns
        empty_default (str): Default value to return for empty/null inputs
        require_wildcard_schema (bool): Whether to only extract database names for wildcard schemas

    Returns:
        str: A regex string in the format ^(name1|name2|...)$ or default values
    """
    try:
        # Handle special cases based on regex type
        if not normalized_regex or normalized_regex == "^$":
            return empty_default

        if normalized_regex == ".*":
            return "'.*'"

        database_names: Set[str] = set()

        # Split by | to get individual patterns
        patterns = normalized_regex.split("|")

        for pattern in patterns:
            try:
                # Skip empty patterns
                if not pattern or not pattern.strip():
                    continue

                # Split by \\. to get database name and schema part
                # The \\. represents an escaped dot in the regex
                parts = pattern.split("\\.")

                # Handle different validation requirements
                if require_wildcard_schema:
                    # For exclude regex, we need at least 2 parts and schema must be wildcard
                    if len(parts) < 2:
                        logger.warning(f"Invalid database name format: {pattern}")
                        continue
                    db_name = parts[0].strip()
                    schema_part = parts[1].strip()
                    # Only extract database name if the schema part is a wildcard (*)
                    if not (
                        db_name and db_name not in (".*", "^$") and schema_part == "*"
                    ):
                        continue
                else:
                    # For include regex, we just need the database name
                    if not parts:
                        continue
                    db_name = parts[0].strip()
                    if not (db_name and db_name not in (".*", "^$")):
                        continue

                # Validate database name format
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_$-]*$", db_name):
                    database_names.add(db_name)
                else:
                    logger.warning(f"Invalid database name format: {db_name}")
            except Exception as e:
                logger.warning(f"Error processing pattern '{pattern}': {str(e)}")
                continue

        if not database_names:
            return empty_default
        return f"'^({'|'.join(sorted(database_names))})$'"

    except Exception as e:
        logger.error(
            f"Error extracting database names from regex '{normalized_regex}': {str(e)}"
        )
        # Return appropriate default based on regex type
        return empty_default


def prepare_query(
    query: Optional[str],
    workflow_args: Dict[str, Any],
    temp_table_regex_sql: Optional[str] = "",
) -> Optional[str]:
    """
    Prepares a SQL query by applying include and exclude filters, and optional
    configurations for temporary table regex, empty tables, and views.

    This function modifies the provided SQL query using filters and settings
    defined in the workflow_args dictionary. The include and exclude filters
    determine which data should be included or excluded from the query. If no
    filters are specified, it fetches all metadata. Temporary table exclusion
    logic is also applied if a regex is provided.

    Args:
        query (str): The base SQL query string to modify with filters.
        workflow_args (Dict[str, Any]): A dictionary containing metadata and workflow-related arguments.
            Expected keys include:
            metadata (dict): A dictionary with the following keys:
            include-filter (str): Regex pattern to include tables/data,
            exclude-filter (str): Regex pattern to exclude tables/data,
            temp-table-regex (str): Regex for temporary tables,
            exclude_empty_tables (bool): Whether to exclude empty tables,
            exclude_views (bool): Whether to exclude views.
        temp_table_regex_sql (str): SQL snippet for excluding temporary tables. Defaults to "".

    Returns:
        Optional[str]: The prepared SQL query with filters applied, or None if an error occurs during preparation.

    """
    try:
        if not query:
            logger.warning("SQL query is not set.")
            return None

        metadata = workflow_args.get("metadata", {})

        # using "or" instead of default correct defaults are set in case of empty string
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

        # Extract database names from the normalized regex patterns
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

        # Use sets directly for SQL query formatting
        exclude_empty_tables = workflow_args.get("metadata", {}).get(
            "exclude_empty_tables", False
        )
        exclude_views = workflow_args.get("metadata", {}).get("exclude_views", False)

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
        # Extract the original error message from the CommonError
        error_message = str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
        logger.error(
            f"Error preparing query [{query}]:  {error_message}",
            error_code=CommonError.QUERY_PREPARATION_ERROR.code,
        )
        return None


def parse_filter_input(
    filter_input: Union[str, Dict[str, Any], None],
) -> Dict[str, Any]:
    """
    Robustly parse filter input from various formats.

    Args:
        filter_input: Can be None, empty string, JSON string, or dict

    Returns:
        Dict[str, Any]: Parsed filter dictionary (empty dict if input is invalid/empty)
    """
    # Handle None or empty cases
    if not filter_input:
        return {}

    # If already a dict, return as-is
    if isinstance(filter_input, dict):
        return filter_input

    # If it's a string, try to parse as JSON
    if isinstance(filter_input, str):
        # Handle empty string
        if not filter_input.strip():
            return {}
        try:
            return json.loads(filter_input)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid filter JSON: '{filter_input}', error: {str(e)}")
            raise CommonError(f"Invalid filter JSON: {str(e)}")


def prepare_filters(
    include_filter_str: str, exclude_filter_str: str
) -> Tuple[str, str]:
    """Prepares the filters for the SQL query.

    Args:
        include_filter_str: The include filter string.
        exclude_filter_str: The exclude filter string.

    Returns:
        tuple: A tuple containing:
            - normalized include regex (str)
            - normalized exclude regex (str)

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
    """Normalizes the filters for the SQL query.

    Args:
        filter_dict: The filter dictionary.
        is_include: Whether the filter is an include filter.

    Returns:
        list: The normalized filter list.

    Examples:
        >>> normalize_filters({"db1": ["schema1", "schema2"], "db2": ["schema3"]}, True)
        ["db1.schema1", "db1.schema2", "db2.schema3"]
        >>> normalize_filters({"db1": "*"}, True)
        ["db1\\.*"]
    """
    normalized_filter_list: List[str] = []
    for filtered_db, filtered_schemas in filter_dict.items():
        db = filtered_db.strip("^$")

        # Handle wildcard case
        if filtered_schemas == "*":
            normalized_filter_list.append(f"{db}\\.*")
            continue

        # Handle empty list case
        if not filtered_schemas:
            normalized_filter_list.append(f"{db}\\.*")
            continue

        # Handle list case
        if isinstance(filtered_schemas, list):
            for schema in filtered_schemas:
                sch = schema.lstrip(
                    "^"
                )  # we do not strip out the $ as it is used to match the end of the string
                normalized_filter_list.append(f"{db}\\.{sch}")

    return normalized_filter_list


def read_sql_files(
    queries_prefix: str = f"{os.path.dirname(os.path.abspath(__file__))}/queries",
) -> Dict[str, str]:
    """
    Reads all SQL files in the queries directory and returns a dictionary of the file name and the SQL content.

    Reads SQL files recursively from the given directory and builds a mapping of filenames
    to their SQL contents. The filenames are converted to uppercase and have the .sql
    extension removed.

    Args:
        queries_prefix: Absolute path of the directory containing SQL query files.

    Returns:
        A dictionary mapping SQL file names (uppercase, without extension) to their contents.
    """
    sql_files: List[str] = glob.glob(
        os.path.join(
            queries_prefix,
            "**/*.sql",
        ),
        recursive=True,
    )

    result: Dict[str, str] = {}
    for file in sql_files:
        with open(file, "r") as f:
            result[os.path.basename(file).upper().replace(".SQL", "")] = (
                f.read().strip()
            )

    return result


def get_actual_cpu_count():
    """Gets the actual number of CPUs available on the system.

    This function attempts to get the true number of CPUs available to the current process
    by checking CPU affinity. Falls back to os.cpu_count() if affinity is not available.

    Returns:
        int: The number of CPUs available to the current process.

    Examples:
        >>> get_actual_cpu_count()
        8  # On a system with 8 CPU cores

        >>> # On a containerized system with CPU limits
        >>> get_actual_cpu_count()
        2  # Returns actual available CPUs rather than host system count

    Note:
        Based on https://stackoverflow.com/a/55423170/1710342
    """
    try:
        return len(os.sched_getaffinity(0)) or 1  # type: ignore
    except AttributeError:
        return os.cpu_count() or 1


def get_safe_num_threads():
    """Gets the recommended number of threads for parallel processing.

    Returns:
        int: The recommended number of threads, calculated as 2x the number of available
            CPU cores, with a minimum of 2 threads.

    Examples:
        >>> get_safe_num_threads()
        16  # On a system with 8 CPU cores

        >>> # On a single core system
        >>> get_safe_num_threads()
        2  # Minimum of 2 threads returned
    """
    return get_actual_cpu_count() * 2 or 2


def parse_credentials_extra(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse the 'extra' field from credentials, handling both string and dict inputs.

    Args:
        credentials (Dict[str, Any]): Credentials dictionary containing an 'extra' field

    Returns:
        Dict[str, Any]: Parsed extra field as a dictionary

    Raises:
        CommonError: If the extra field contains invalid JSON

    NOTE:
        This helper function is added considering the structure of the credentials
        format in the argo/cross-over workflows world.
        This is bound to change in the future.
    """
    extra: Union[str, Dict[str, Any]] = credentials.get("extra", {})

    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except json.JSONDecodeError as e:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid JSON in credentials extra field: {e}"
            )

    return extra  # We know it's a Dict[str, Any] due to the Union type and str check


def run_sync(func):
    """Run a function in a thread pool executor.

    Args:
        func: The function to run in thread pool.

    Returns:
        An async wrapper function that runs the input function in a thread pool.
    """

    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as pool:
            return await loop.run_in_executor(pool, func, *args, **kwargs)

    return wrapper
