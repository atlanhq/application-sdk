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
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.statestore import StateStoreOutput

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def extract_database_names_from_regex(normalized_regex: str) -> str:
    """
    Extract database names from normalized regex patterns and return a regex string suitable for SQL queries.

    This function parses regex patterns like 'dev\\.external_schema$|wide_world_importers\\.bronze_sales$'
    or 'dev\\.*|wide_world_importers\\.*' to extract the database names, and returns a regex string
    like '^(dev|wide_world_importers)$' for use in SQL queries.

    Args:
        normalized_regex (str): The normalized regex pattern containing database.schema patterns

    Returns:
        str: A regex string in the format ^(name1|name2|...)$ or '^$' if no names are found.

    Examples:
        >>> extract_database_names_from_regex('dev\\.external_schema$|wide_world_importers\\.bronze_sales$')
        '^(dev|wide_world_importers)$'
        >>> extract_database_names_from_regex('dev\\.*|wide_world_importers\\.*')
        '^(dev|wide_world_importers)$'
        >>> extract_database_names_from_regex('^$')
        '^$'

    Raises:
        CommonError: If the input is invalid or processing fails
    """
    try:
        if not normalized_regex or not isinstance(normalized_regex, str):
            logger.warning("Invalid normalized_regex input: empty or non-string value")
            return "'^$'"

        database_names: Set[str] = set()

        # Split by | to get individual patterns
        patterns = normalized_regex.split("|")

        for pattern in patterns:
            try:
                # Skip empty patterns
                if not pattern or not pattern.strip():
                    continue

                # Split by \\. to get database name (first part)
                # The \\. represents an escaped dot in the regex
                parts = pattern.split("\\.")
                if parts:
                    db_name = parts[0].strip()
                    if db_name and db_name not in (".*", "^$"):
                        # Validate database name format
                        if re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", db_name):
                            database_names.add(db_name)
                        else:
                            logger.warning(f"Invalid database name format: {db_name}")

            except Exception as e:
                logger.warning(f"Error processing pattern '{pattern}': {str(e)}")
                continue

        if not database_names:
            return "'^$'"
        return f"'^({'|'.join(sorted(database_names))})$'"

    except Exception as e:
        logger.error(
            f"Error extracting database names from regex '{normalized_regex}': {str(e)}"
        )
        # Return a safe default that excludes everything
        return "'^$'"


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
        include_databases = extract_database_names_from_regex(normalized_include_regex)
        exclude_databases = extract_database_names_from_regex(normalized_exclude_regex)

        if include_databases == "'^$'" and exclude_databases == "'^$'":
            include_databases = "'.*'"
            exclude_databases = "'^$'"

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
    try:
        include_filter = json.loads(include_filter_str)
    except json.JSONDecodeError as e:
        raise CommonError(f"Invalid include filter JSON: {str(e)}")

    try:
        exclude_filter = json.loads(exclude_filter_str)
    except json.JSONDecodeError as e:
        raise CommonError(f"Invalid exclude filter JSON: {str(e)}")

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


def get_workflow_config(config_id: str) -> Dict[str, Any]:
    """Gets the workflow configuration from the state store using config id.

    Args:
        config_id: The configuration ID to retrieve.

    Returns:
        dict: The workflow configuration.
    """
    return StateStoreInput.extract_configuration(config_id)


def update_workflow_config(config_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Updates the workflow configuration.

    Args:
        config_id: The configuration ID to update.
        config: The new configuration dictionary.

    Returns:
        dict: The updated workflow configuration.
    """
    extracted_config = get_workflow_config(config_id)

    for key in extracted_config.keys():
        if key in config and config[key] is not None:
            extracted_config[key] = config[key]

    StateStoreOutput.store_configuration(config_id, extracted_config)
    return extracted_config


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
