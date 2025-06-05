"""Utility functions for transformers.

This module provides common utility functions used across different transformers
for text processing and URI/name building.
"""

import glob
import os
import re
from typing import Any, Dict, List, Optional


def process_text(text: str, max_length: int = 100000) -> str:
    """Process and sanitize text for storage.

    This function processes text by:
    1. Truncating it to a maximum length
    2. Removing HTML tags
    3. Converting to a JSON-safe string

    Args:
        text (str): The text to process.
        max_length (int, optional): Maximum length of text to keep. Defaults to 100000.

    Returns:
        str: The processed text, truncated and sanitized.
    """
    if len(text) > max_length:
        text = text[:max_length]

    # Remove HTML tags
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()

    return text


def build_phoenix_uri(connector_name: str, connector_type: str, *args: str) -> str:
    """Build the URI for a Phoenix entity.

    This function constructs a URI path by joining the connector name, type,
    and additional path components.

    Args:
        connector_name (str): Name of the connector.
        connector_type (str): Type of the connector.
        *args (str): Additional path components to append.

    Returns:
        str: The constructed URI path, starting with a forward slash.

    Example:
        >>> build_phoenix_uri("myconnector", "sql", "db", "schema")
        '/myconnector/sql/db/schema'
    """
    return f"/{connector_name}/{connector_type}/{'/'.join(args)}"


def build_atlas_qualified_name(connection_qualified_name: str, *args: str) -> str:
    """Build the qualified name for an Atlas entity.

    This function constructs a qualified name by joining the connection qualified name
    and additional name components.

    Args:
        connection_qualified_name (str): Base qualified name for the connection.
        *args (str): Additional name components to append.

    Returns:
        str: The constructed qualified name with components joined by forward slashes.

    Example:
        >>> build_atlas_qualified_name("tenant/connector/1", "db", "schema")
        'tenant/connector/1/db/schema'
    """
    return f"{connection_qualified_name}/{'/'.join(args)}"


def get_yaml_query_template_path_mappings(
    custom_templates_path: Optional[str] = None,
    assets: Optional[List[str]] = None,
) -> Dict[str, str]:
    """
    Returns a dictionary mapping of data assets (TABLE, COLUMN, DATABASE, SCHEMA)
    to the path of the YAML file that contains the SQL query template.

    Args:
        custom_templates_path: The path of the directory containing the YAML files. To be used for custom templates.

    Returns:
        A dictionary mapping of data assets to the path of the YAML file that contains the SQL query template.
    """
    default_yaml_files: List[str] = glob.glob(
        os.path.join(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "query/templates",
            ),
            "**/*.yaml",
        ),
        recursive=True,
    )

    yaml_files: List[str] = (
        glob.glob(
            os.path.join(
                custom_templates_path,
                "**/*.yaml",
            ),
            recursive=True,
        )
        if custom_templates_path
        else []
    )

    result: Dict[str, str] = {}
    for file in default_yaml_files + yaml_files:
        file_name = os.path.basename(file).upper().replace(".YAML", "")
        if not assets or (assets and file_name in assets):
            result[file_name] = file

    return result


def flatten_yaml_columns(
    nested: dict, parent_key: str = "", sep: str = "."
) -> list[dict]:
    """
    Recursively flattens a nested columns dictionary into a list of dicts
    with dot-separated keys for the 'name' field, suitable for SQL processing.

    Args:
        nested (dict): The nested columns dictionary.
        parent_key (str): The prefix for the current level.
        sep (str): The separator to use (default: ".").

    Returns:
        list[dict]: A flat list of column definitions with dot notation.
    """
    flat_columns: List[Dict[str, Any]] = []
    for key, value in nested.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict) and any(isinstance(v, dict) for v in value.values()):
            # If value is a dict and has nested dicts, recurse
            flat_columns.extend(flatten_yaml_columns(value, new_key, sep=sep))
        else:
            # This is a leaf node (column definition)
            col_def = (
                value.copy() if isinstance(value, dict) else {"source_query": value}
            )
            col_def["name"] = new_key
            flat_columns.append(col_def)
    return flat_columns
