from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

from application_sdk.activities.common.models import ActivityStatistics
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.common.utils import (
    get_database_names,
    parse_credentials_extra,
    prepare_query,
)
from application_sdk.io.parquet import ParquetFileWriter
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    import pandas as pd

logger = get_logger(__name__)


async def setup_database_connection(
    sql_client: BaseSQLClient,
    database_name: str,
) -> None:
    """Setup connection for a specific database.

    Args:
        sql_client: The SQL client to configure.
        database_name: The name of the database to connect to.
    """
    extra = parse_credentials_extra(sql_client.credentials)
    extra["database"] = database_name
    sql_client.credentials["extra"] = extra
    await sql_client.load(sql_client.credentials)


def prepare_database_query(
    sql_query: str,
    database_name: Optional[str],
    workflow_args: Dict[str, Any],
    temp_table_regex_sql: str = "",
    use_posix_regex: bool = False,
) -> str:
    """Prepare query for database execution with proper substitutions.

    Args:
        sql_query: The raw SQL query string.
        database_name: The database name to substitute into the query.
        workflow_args: Workflow arguments for query preparation.
        temp_table_regex_sql: SQL regex for temp table exclusion.
        use_posix_regex: Whether to use POSIX regex syntax.

    Returns:
        The prepared SQL query string.

    Raises:
        ValueError: If query preparation fails.
    """
    # Replace database name placeholder if provided
    fetch_sql = sql_query
    if database_name:
        fetch_sql = fetch_sql.replace("{database_name}", database_name)

    # Prepare the query
    prepared_query = prepare_query(
        query=fetch_sql,
        workflow_args=workflow_args,
        temp_table_regex_sql=temp_table_regex_sql,
        use_posix_regex=use_posix_regex,
    )

    if prepared_query is None:
        db_context = f" for database {database_name}" if database_name else ""
        raise ValueError(f"Failed to prepare query{db_context}")

    return prepared_query


async def execute_single_db(
    sql_client: BaseSQLClient,
    prepared_query: Optional[str],
    parquet_output: Optional[ParquetFileWriter],
    write_to_file: bool,
) -> Tuple[
    bool, Optional[Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]]
]:
    """Execute a query against a single database.

    Args:
        sql_client: The SQL client to use.
        prepared_query: The prepared SQL query to execute.
        parquet_output: Optional parquet writer for output.
        write_to_file: Whether to write results to file.

    Returns:
        Tuple of (success boolean, optional result iterator).
    """
    if not prepared_query:
        logger.error("Prepared query is None, cannot execute")
        return False, None

    try:
        batched_iterator = await sql_client.get_batched_results(prepared_query)

        if write_to_file and parquet_output:
            await parquet_output.write_batches(batched_iterator)  # type: ignore
            return True, None

        return True, batched_iterator
    except Exception as e:
        logger.error(
            f"Error during query execution or output writing: {e}", exc_info=True
        )
        raise


async def finalize_multidb_results(
    write_to_file: bool,
    concatenate: bool,
    return_dataframe: bool,
    parquet_output: Optional[ParquetFileWriter],
    dataframe_list: List[
        Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]
    ],
    setup_parquet_output_func: Callable[
        [str, bool, Optional[str]], Optional[ParquetFileWriter]
    ],
    output_path: str,
    typename: str,
) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]:
    """Finalize results for multi-database execution.

    Args:
        write_to_file: Whether results were written to file.
        concatenate: Whether to concatenate in-memory results.
        return_dataframe: Whether to return the concatenated dataframe.
        parquet_output: The parquet writer used (if any).
        dataframe_list: List of dataframe iterators from each DB.
        setup_parquet_output_func: Callback to create new parquet output.
        output_path: Full path for output files.
        typename: Type name for statistics.

    Returns:
        Statistics or DataFrame, or None.
    """
    if write_to_file and parquet_output:
        return await parquet_output.close()

    if not write_to_file and concatenate:
        try:
            import pandas as pd

            valid_dataframes: List[pd.DataFrame] = []
            for df_generator in dataframe_list:
                if df_generator is None:
                    continue
                # Handle both async and sync iterators
                if hasattr(df_generator, "__aiter__"):
                    async for dataframe in df_generator:  # type: ignore
                        if dataframe is None or (
                            hasattr(dataframe, "empty") and dataframe.empty
                        ):
                            continue
                        valid_dataframes.append(dataframe)
                else:
                    for dataframe in df_generator:  # type: ignore
                        if dataframe is None or (
                            hasattr(dataframe, "empty") and dataframe.empty
                        ):
                            continue
                        valid_dataframes.append(dataframe)

            if not valid_dataframes:
                logger.warning(
                    "No valid dataframes collected across databases for concatenation"
                )
                return None

            concatenated = pd.concat(valid_dataframes, ignore_index=True)

            if return_dataframe:
                return concatenated

            # Create new parquet output for concatenated data
            concatenated_parquet_output = setup_parquet_output_func(
                output_path, True, typename
            )
            if concatenated_parquet_output:
                await concatenated_parquet_output.write(concatenated)  # type: ignore
                return await concatenated_parquet_output.close()
        except Exception as e:
            logger.error(
                f"Error concatenating multi-DB dataframes: {str(e)}",
                exc_info=True,
            )
            raise

    logger.warning(
        "multidb execution returned no output to write (write_to_file=False, concatenate=False)"
    )
    return None


async def execute_multidb_flow(
    sql_client: BaseSQLClient,
    sql_query: str,
    workflow_args: Dict[str, Any],
    fetch_database_sql: Optional[str],
    output_path: str,
    typename: str,
    write_to_file: bool,
    concatenate: bool,
    return_dataframe: bool,
    parquet_output: Optional[ParquetFileWriter],
    temp_table_regex_sql: str,
    setup_parquet_output_func: Callable[[str, bool], Optional[ParquetFileWriter]],
) -> Optional[Union[ActivityStatistics, "pd.DataFrame"]]:
    """Execute multi-database flow with proper error handling and result finalization.

    Args:
        sql_client: The SQL client to use.
        sql_query: The SQL query to execute on each database.
        workflow_args: Workflow arguments.
        fetch_database_sql: SQL to fetch list of databases.
        output_path: Full path for output files.
        typename: Type name for statistics.
        write_to_file: Whether to write results to file.
        concatenate: Whether to concatenate in-memory results.
        return_dataframe: Whether to return the concatenated dataframe.
        parquet_output: The parquet writer used (if any).
        temp_table_regex_sql: SQL regex for temp table exclusion.
        setup_parquet_output_func: Callback to create new parquet output.

    Returns:
        Statistics or DataFrame, or None.
    """
    # Resolve databases to iterate
    database_names = await get_database_names(
        sql_client, workflow_args, fetch_database_sql
    )
    if not database_names:
        logger.warning("No databases found to process")
        return None

    successful_databases: List[str] = []
    dataframe_list: List[
        Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]
    ] = []

    # Iterate databases and execute
    for database_name in database_names or []:
        try:
            # Setup connection for this database
            await setup_database_connection(sql_client, database_name)

            # Prepare query for this database
            prepared_query = prepare_database_query(
                sql_query,
                database_name,
                workflow_args,
                temp_table_regex_sql,
                use_posix_regex=True,
            )

            # Execute using helper method
            success, batched_iterator = await execute_single_db(
                sql_client,
                prepared_query,
                parquet_output,
                write_to_file,
            )

            if success:
                logger.info(f"Successfully processed database: {database_name}")

        except Exception as e:
            logger.error(
                f"Failed to process database '{database_name}': {str(e)}. Failing the workflow.",
                exc_info=True,
            )
            raise

        if success:
            successful_databases.append(database_name)
            if not write_to_file and batched_iterator:
                dataframe_list.append(batched_iterator)

    # Log results
    logger.info(
        f"Successfully processed {len(successful_databases)} databases: {successful_databases}"
    )

    # Finalize results
    return await finalize_multidb_results(
        write_to_file,
        concatenate,
        return_dataframe,
        parquet_output,
        dataframe_list,
        setup_parquet_output_func,
        output_path,
        typename,
    )
