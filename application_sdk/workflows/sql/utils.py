import json
import logging
from typing import Any, Dict, List, Tuple

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


def prepare_query(query: str, workflow_args: Dict[str, Any]) -> str:
    """
    Method to prepare the query with the include and exclude filters.
    Only fetches all metadata when both include and exclude filters are empty.
    """
    try:
        metadata = workflow_args.get("metadata", workflow_args.get("form_data", {}))

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
