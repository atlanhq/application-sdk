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

from sqlalchemy.exc import OperationalError

from application_sdk.activities.common.utils import get_object_store_prefix
from application_sdk.common.error_codes import CommonError
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.parquet import ParquetOutput
from application_sdk.services.objectstore import ObjectStore

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


def transform_posix_regex(regex_pattern: str) -> str:
    """
    Transform regex pattern for POSIX compatibility.

    Rules:
    1. Add ^ before each database name before \.
    2. Add an additional . between \. and * if * follows \.

    Example: 'dev\.public$|dev\.atlan_test_schema$|wide_world_importers\.*'
    Becomes: '^dev\.public$|^dev\.atlan_test_schema$|^wide_world_importers\..*'
    """
    if not regex_pattern:
        return regex_pattern

    # Split by | to handle each pattern separately
    patterns = regex_pattern.split("|")
    transformed_patterns = []

    for pattern in patterns:
        # Add ^ at the beginning if it's not already there
        if not pattern.startswith("^"):
            pattern = "^" + pattern

            # Add additional . between \. and * if * follows \.
            pattern = re.sub(r"\\\.\*", r"\..*", pattern)

        transformed_patterns.append(pattern)

    return "|".join(transformed_patterns)


def prepare_query(
    query: Optional[str],
    workflow_args: Dict[str, Any],
    temp_table_regex_sql: Optional[str] = "",
    use_posix_regex: Optional[bool] = False,
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

        if use_posix_regex:
            normalized_include_regex_posix = transform_posix_regex(
                normalized_include_regex
            )
            normalized_exclude_regex_posix = transform_posix_regex(
                normalized_exclude_regex
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
        # Extract the original error message from the CommonError
        error_message = str(e).split(": ", 1)[-1] if ": " in str(e) else str(e)
        logger.error(
            f"Error preparing query [{query}]:  {error_message}",
            error_code=CommonError.QUERY_PREPARATION_ERROR.code,
        )
        return None


async def get_database_names(
    sql_client, workflow_args, fetch_database_sql
) -> Optional[List[str]]:
    """
    Get the database names from the workflow args if include-filter is present
    Args:
        workflow_args: The workflow args
    Returns:
        List[str]: The database names
    """
    database_names = parse_filter_input(
        workflow_args.get("metadata", {}).get("include-filter", {})
    )

    database_names = [
        re.sub(r"^[^\w]+|[^\w]+$", "", database_name)
        for database_name in database_names
    ]
    if not database_names:
        # if database_names are not provided in the include-filter, we'll run the query to get all the database names
        # because by default for an empty include-filter, we fetch details corresponding to all the databases.
        temp_table_regex_sql = workflow_args.get("metadata", {}).get(
            "temp-table-regex", ""
        )
        prepared_query = prepare_query(
            query=fetch_database_sql,
            workflow_args=workflow_args,
            temp_table_regex_sql=temp_table_regex_sql,
            use_posix_regex=True,
        )
        # We'll run the query to get all the database names
        if prepared_query is None:
            logger.error("Failed to prepare database discovery query")
            return []
        database_sql_input = SQLQueryInput(
            engine=sql_client.engine,
            query=prepared_query,
            chunk_size=None,
        )
        database_dataframe = await database_sql_input.get_dataframe()
        database_names = list(database_dataframe["database_name"])
    return database_names


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


def has_custom_control_config(workflow_args: Dict[str, Any]) -> bool:
    """
    Check if custom control configuration is present in workflow arguments.

    Args:
        workflow_args: The workflow arguments

    Returns:
        bool: True if custom control configuration is present, False otherwise
    """
    return (
        workflow_args.get("control-config-strategy") == "custom"
        and workflow_args.get("control-config") is not None
    )


async def get_file_names(output_path: str, typename: str) -> List[str]:
    """
    Get file names for a specific asset type from the transformed directory.

    Args:
        output_path (str): The base output path
        typename (str): The asset type (e.g., 'table', 'schema', 'column')

    Returns:
        List[str]: List of relative file paths for the asset type
    """

    source = get_object_store_prefix(os.path.join(output_path, typename))
    await ObjectStore.download_prefix(source, TEMPORARY_PATH)

    file_pattern = os.path.join(output_path, typename, "*.json")
    file_names = glob.glob(file_pattern)
    file_name_list = [
        "/".join(file_name.rsplit("/", 2)[-2:]) for file_name in file_names
    ]

    return file_name_list


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


async def _setup_database_connection(sql_client, database_name: str) -> None:
    """
    Set up database connection for a specific database.

    Args:
        sql_client: The SQL client instance
        database_name: Name of the database to connect to

    Raises:
        ValueError: If credentials parsing fails
    """
    # Parse extra field if it's a JSON string
    if isinstance(sql_client.credentials.get("extra"), str):
        try:
            extra_dict = json.loads(sql_client.credentials["extra"])
            extra_dict["database"] = database_name
            sql_client.credentials["extra"] = extra_dict
        except json.JSONDecodeError:
            logger.error(
                f"Failed to parse extra field as JSON: {sql_client.credentials.get('extra')}"
            )
            raise ValueError("Invalid JSON in extra field")
    else:
        sql_client.credentials["extra"]["database"] = database_name

    await sql_client.load(sql_client.credentials)


def _prepare_database_query(
    sql_query: str,
    database_name: str,
    typename: str,
    extract_temp_table_regex_column_sql: str,
    extract_temp_table_regex_table_sql: str,
    workflow_args: Dict[str, Any],
) -> str:
    """
    Prepare the SQL query for a specific database.

    Args:
        sql_query: The base SQL query template
        database_name: Name of the database
        typename: Type of metadata being extracted
        extract_temp_table_regex_column_sql: SQL for column temp table regex
        extract_temp_table_regex_table_sql: SQL for table temp table regex
        workflow_args: Workflow arguments for query preparation

    Returns:
        Prepared SQL query ready for execution
    """
    fetch_sql = sql_query.replace("{database_name}", database_name)

    # Select appropriate temp table regex SQL based on typename
    if typename == "column":
        temp_table_regex_sql = extract_temp_table_regex_column_sql
    elif typename == "table":
        temp_table_regex_sql = extract_temp_table_regex_table_sql
    else:
        temp_table_regex_sql = ""

    prepared_query = prepare_query(
        query=fetch_sql,
        workflow_args=workflow_args,
        temp_table_regex_sql=temp_table_regex_sql,
        use_posix_regex=True,
    )

    if prepared_query is None:
        raise ValueError(f"Failed to prepare query for database {database_name}")

    return prepared_query


async def _process_single_database(
    sql_client,
    database_name: str,
    prepared_query: str,
    parquet_output: Optional[ParquetOutput],
    write_to_file: bool,
) -> Tuple[bool, Optional[Any]]:
    """
    Process a single database by executing the query and handling results.

    Args:
        sql_client: The SQL client instance
        database_name: Name of the database being processed
        prepared_query: The prepared SQL query
        parquet_output: Parquet output handler (if writing to file)
        write_to_file: Whether to write results to file

    Returns:
        Tuple[bool, Optional[Any]]: A tuple containing success status and dataframe or None
    """
    try:
        sql_input = SQLQueryInput(
            engine=sql_client.engine,  # type: ignore
            query=prepared_query,  # type: ignore
        )
        dataframe = await sql_input.get_batched_dataframe()

        if write_to_file and parquet_output:
            await parquet_output.write_batched_dataframe(dataframe)  # type: ignore
            return True, None
        else:
            return True, dataframe

    except OperationalError as e:
        error_msg = str(e)
        logger.warning(
            f"Failed to connect to database '{database_name}': {error_msg}. Skipping to next database."
        )
        return False, None
    except Exception as e:
        logger.warning(
            f"Unexpected error processing database '{database_name}': {str(e)}. Skipping to next database."
        )
        return False, None


async def _handle_final_results(
    write_to_file: bool,
    parquet_output: Optional[ParquetOutput],
    dataframe_list: List[Any],
    typename: str,
) -> Any:
    """
    Handle final result processing and return appropriate statistics or data.

    Args:
        write_to_file: Whether results were written to file
        parquet_output: Parquet output handler
        dataframe_list: List of dataframes (if not writing to file)
        typename: Type of metadata for statistics

    Returns:
        Statistics (if writing to file) or list of dataframes
    """
    if write_to_file and parquet_output:
        statistics = await parquet_output.get_statistics(typename=typename)
        return statistics
    else:
        return dataframe_list


async def multidb_query_executor(
    sql_client,
    fetch_database_sql,
    extract_temp_table_regex_column_sql,
    extract_temp_table_regex_table_sql,
    sql_query: Optional[str],
    workflow_args: Dict[str, Any],
    output_suffix: str,
    typename: str,
    write_to_file: bool = True,
):
    """
    Create new connection for each database and execute the query and write the dataframe.

    This function orchestrates the processing of multiple databases by:
    1. Getting the list of databases to process
    2. Setting up output handling
    3. Processing each database individually
    4. Logging results and returning appropriate data

    Args:
        sql_client: The SQL client instance
        fetch_database_sql: SQL query to fetch database names
        extract_temp_table_regex_column_sql: SQL for column temp table regex
        extract_temp_table_regex_table_sql: SQL for table temp table regex
        sql_query: The SQL query template to execute
        workflow_args: Workflow arguments
        output_suffix: Suffix for output files
        typename: Type of metadata being extracted
        write_to_file: Whether to write results to file

    Returns:
        Statistics (if writing to file) or list of dataframes

    Raises:
        ValueError: If SQL client is not initialized or output paths are missing
    """
    # Get list of databases to process
    database_names = await get_database_names(
        sql_client, workflow_args, fetch_database_sql
    )

    # Validate SQL client
    if not sql_client or not sql_client.engine:
        logger.error("SQL client or engine not initialized")
        raise ValueError("SQL client or engine not initialized")

    # Set up output handling
    parquet_output = None
    if write_to_file:
        output_prefix = workflow_args.get("output_prefix")
        output_path = workflow_args.get("output_path")
        if not output_prefix or not output_path:
            logger.error("Output prefix or path not provided in workflow_args.")
            raise ValueError(
                "Output prefix and path must be specified in workflow_args."
            )
        parquet_output = ParquetOutput(
            output_prefix=output_prefix,
            output_path=output_path,
            output_suffix=output_suffix,
        )

    # Process each database
    successful_databases = []
    failed_databases = []
    dataframe_list = []

    for database_name in database_names or []:
        try:
            # Set up database connection
            await _setup_database_connection(sql_client, database_name)

            # Prepare query for this database
            prepared_query = _prepare_database_query(
                sql_query=sql_query,  # type: ignore
                database_name=database_name,
                typename=typename,
                extract_temp_table_regex_column_sql=extract_temp_table_regex_column_sql,
                extract_temp_table_regex_table_sql=extract_temp_table_regex_table_sql,
                workflow_args=workflow_args,
            )

            # Process the database
            success, dataframe = await _process_single_database(
                sql_client=sql_client,
                database_name=database_name,
                prepared_query=prepared_query,
                parquet_output=parquet_output,
                write_to_file=write_to_file,
            )

            if success:
                successful_databases.append(database_name)
                logger.info(f"Successfully processed database: {database_name}")
                if not write_to_file and dataframe is not None:
                    dataframe_list.append(dataframe)
            else:
                failed_databases.append(database_name)

        except Exception as e:
            logger.warning(
                f"Failed to process database '{database_name}': {str(e)}. Skipping to next database."
            )
            failed_databases.append(database_name)
            continue

    # Log processing summary
    logger.info(
        f"Successfully processed {len(successful_databases)} databases: {successful_databases}"
    )
    logger.warning(
        f"Failed to process {len(failed_databases)} databases: {failed_databases}"
    )
    # Handle final results
    return await _handle_final_results(
        write_to_file=write_to_file,
        parquet_output=parquet_output,
        dataframe_list=dataframe_list,
        typename=typename,
    )
