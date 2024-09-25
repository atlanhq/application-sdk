import json
from typing import Dict, List, Tuple


def prepare_filters(
    include_filter_str: str, exclude_filter_str: str, temp_table_regex_str: str
) -> Tuple[str, str, str]:
    """
    Prepare the filters for the SQL query.

    Args:
        include_filter_str (str): The include filter string.
        exclude_filter_str (str): The exclude filter string.
        temp_table_regex_str (str): The temporary table regex string.

    Returns:
        Tuple[str, str, str]: The normalized include regex, the normalized exclude regex, and the exclude table.
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
        else "$^"
    )

    exclude_table = temp_table_regex_str if temp_table_regex_str else "$^"

    return normalized_include_regex, normalized_exclude_regex, exclude_table


def normalize_filters(filter_dict: Dict[str, List[str]], is_include: bool) -> List[str]:
    """
    Normalize the filters for the SQL query.

    Args:
        filter_dict (Dict[str, List[str]]): The filter dictionary.
        is_include (bool): Whether the filter is an include filter.

    Returns:
        List[str]: The normalized filter list.
    """
    normalized_filter_list: List[str] = []
    for filtered_db, filtered_schemas in filter_dict.items():
        db = filtered_db.strip("^$")
        if not filtered_schemas:
            normalized_filter_list.append(f"{db}.*")
        else:
            for schema in filtered_schemas:
                sch = schema.lstrip(
                    "^"
                )  # we do not strip out the $ as it is used to match the end of the string
                normalized_filter_list.append(f"{db}.{sch}")

    return normalized_filter_list
