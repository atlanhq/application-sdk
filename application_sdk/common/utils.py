import json
from typing import Any, Awaitable, Callable, Dict, List, Tuple, TypeVar

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs.statestore import StateStore

logger = get_logger()

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def prepare_query(
    query: str, workflow_args: Dict[str, Any], temp_table_regex_sql: str = ""
) -> str:
    """
    Prepares a SQL query by applying include and exclude filters, and optional
    configurations for temporary table regex, empty tables, and views.

    This function modifies the provided SQL query using filters and settings
    defined in the `workflow_args` dictionary. The include and exclude filters
    determine which data should be included or excluded from the query. If no
    filters are specified, it fetches all metadata. Temporary table exclusion
    logic is also applied if a regex is provided.

    Args:
        query (str): The base SQL query string to modify with filters.
        workflow_args (Dict[str, Any]): A dictionary containing metadata and
            workflow-related arguments. Expected keys include:
            - "metadata": A dictionary with the following keys:
                - "include-filter" (str): Regex pattern to include tables/data.
                - "exclude-filter" (str): Regex pattern to exclude tables/data.
                - "temp-table-regex" (str): Regex for temporary tables.
                - "exclude_empty_tables" (bool): Whether to exclude empty tables.
                - "exclude_views" (bool): Whether to exclude views.
        temp_table_regex_sql (str, optional): SQL snippet for excluding temporary
            tables. Defaults to an empty string.

    Returns:
        str: The prepared SQL query with filters applied, or `None` if an error
        occurs during preparation.

    Raises:
        Exception: Logs the error message and returns `None` if query preparation fails.
    """
    try:
        metadata = workflow_args.get("metadata", {})

        # using "or" instead of default correct defaults are set in case of empty string
        include_filter = metadata.get("include-filter") or "{}"
        exclude_filter = metadata.get("exclude-filter") or "{}"
        if metadata.get("temp-table-regex"):
            temp_table_regex_sql = temp_table_regex_sql.format(
                exclude_table_regex=metadata.get("temp-table-regex")
            )
        else:
            temp_table_regex_sql = ""

        normalized_include_regex, normalized_exclude_regex = prepare_filters(
            include_filter, exclude_filter
        )

        exclude_empty_tables = workflow_args.get("metadata", {}).get(
            "exclude_empty_tables", False
        )
        exclude_views = workflow_args.get("metadata", {}).get("exclude_views", False)

        return query.format(
            normalized_include_regex=normalized_include_regex,
            normalized_exclude_regex=normalized_exclude_regex,
            temp_table_regex_sql=temp_table_regex_sql,
            exclude_empty_tables=exclude_empty_tables,
            exclude_views=exclude_views,
        )
    except Exception as e:
        logger.error(f"Error preparing query [{query}]:  {e}")
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
    """
    include_filter = json.loads(include_filter_str)
    exclude_filter = json.loads(exclude_filter_str)

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
    return StateStore.extract_configuration(config_id)


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

    StateStore.store_configuration(config_id, extracted_config)
    return extracted_config
