import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Tuple, TypeVar

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStore

logger = AtlanLoggerAdapter(logging.getLogger(__name__))

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def prepare_query(query: str, workflow_args: Dict[str, Any]) -> str:
    """
    Method to prepare the query with the include and exclude filters.
    Only fetches all metadata when both include and exclude filters are empty.
    """
    try:
        metadata = workflow_args.get("metadata", {})

        # using "or" instead of default correct defaults are set in case of empty string
        include_filter = metadata.get("include_filter") or "{}"
        exclude_filter = metadata.get("exclude_filter") or "{}"
        temp_table_regex = metadata.get("temp_table_regex") or "^$"

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
            exclude_table=temp_table_regex,
            exclude_empty_tables=exclude_empty_tables,
            exclude_views=exclude_views,
        )
    except Exception as e:
        logger.error(f"Error preparing query [{query}]:  {e}")
        return None


def prepare_filters(
    include_filter_str: str, exclude_filter_str: str
) -> Tuple[str, str]:
    """
    Prepare the filters for the SQL query.

    :param include_filter_str: The include filter string.
    :param exclude_filter_str: The exclude filter string.
    :return: The normalized include regex, the normalized exclude regex, and the exclude table.
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
    """
    Normalize the filters for the SQL query.

    :param filter_dict: The filter dictionary.
    :param is_include: Whether the filter is an include filter.
    :return: The normalized filter list.

    Usage:
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
    """
    Method to get the workflow configuration from the state store using config id
    """
    return StateStore.extract_configuration(config_id)


def update_workflow_config(config_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Method to update the workflow config
    """
    extracted_config = get_workflow_config(config_id)

    for key in extracted_config.keys():
        if key in config and config[key] is not None:
            extracted_config[key] = config[key]

    StateStore.store_configuration(config_id, extracted_config)
    return extracted_config
