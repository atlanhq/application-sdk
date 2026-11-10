"""Utility functions for the deprecated transformers.

.. deprecated:: 3.56.0
    Every public function here is deprecated and removed in v4.0 with the rest
    of ``application_sdk.transformers``. The YAML-template helpers moved to
    :mod:`application_sdk.common.yaml_templates`; the rest have no SDK
    replacement (see each function's notice).

Each public function is a ``@deprecated`` wrapper over a private
implementation. The SDK's own deprecated transformers (``transformers.atlas``,
``transformers.query``) call the private ones, so an app still on those
transformers is not handed a warning per row for a call it never wrote — the
transformer's own class-level warning already tells it to migrate.
"""

import glob
import os
import re
from typing import Dict, List, Optional

from typing_extensions import deprecated

from application_sdk.common.yaml_templates import (
    flatten_yaml_columns as _flatten_yaml_columns,
)


def _process_text(text: str, max_length: int = 100000) -> str:
    if len(text) > max_length:
        text = text[:max_length]

    # Remove HTML tags
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()

    return text


@deprecated(
    "application_sdk.transformers.common.utils.process_text is deprecated; "
    "use the connector-side asset-mapper pattern (typed records → map_<entity>() "
    "→ pyatlan_v9 Asset) and clean text in the mapper instead — will be removed "
    "in v4.0. See docs/upgrade-guide-v3.md."
)
def process_text(text: str, max_length: int = 100000) -> str:
    """Process and sanitize text for storage.

    Truncates *text* to *max_length*, strips HTML tags, and collapses
    whitespace.

    Args:
        text (str): The text to process.
        max_length (int, optional): Maximum length of text to keep. Defaults to 100000.

    Returns:
        str: The processed text, truncated and sanitized.
    """
    return _process_text(text, max_length)


@deprecated(
    "application_sdk.transformers.common.utils.build_phoenix_uri is deprecated; "
    "use an f-string in the app instead — will be removed in v4.0."
)
def build_phoenix_uri(connector_name: str, connector_type: str, *args: str) -> str:
    """Build the URI for a Phoenix entity.

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


def _build_atlas_qualified_name(connection_qualified_name: str, *args: str) -> str:
    return f"{connection_qualified_name}/{'/'.join(args)}"


@deprecated(
    "application_sdk.transformers.common.utils.build_atlas_qualified_name is "
    "deprecated; use the pyatlan_v9 creator instead "
    "(<Asset>.creator(...).qualified_name), which owns the qualifiedName grammar "
    "— will be removed in v4.0."
)
def build_atlas_qualified_name(connection_qualified_name: str, *args: str) -> str:
    """Build the qualified name for an Atlas entity.

    Args:
        connection_qualified_name (str): Base qualified name for the connection.
        *args (str): Additional name components to append.

    Returns:
        str: The constructed qualified name with components joined by forward slashes.

    Example:
        >>> build_atlas_qualified_name("tenant/connector/1", "db", "schema")
        'tenant/connector/1/db/schema'
    """
    return _build_atlas_qualified_name(connection_qualified_name, *args)


def _get_yaml_query_template_path_mappings(
    custom_templates_path: Optional[str] = None,
    assets: Optional[List[str]] = None,
) -> Dict[str, str]:
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


@deprecated(
    "application_sdk.transformers.common.utils.get_yaml_query_template_path_mappings "
    "is deprecated; use application_sdk.common.yaml_templates.yaml_template_paths "
    "instead, which resolves only your app's template directory (copy any SDK "
    "default template you rely on into it) — will be removed in v4.0."
)
def get_yaml_query_template_path_mappings(
    custom_templates_path: Optional[str] = None,
    assets: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Map each data asset (TABLE, COLUMN, …) to its YAML query template path.

    Template resolution order (later entries override earlier ones by filename):
        1. SDK default templates (query/templates/)
        2. App custom templates (custom_templates_path) - if provided

    Args:
        custom_templates_path: Path to app-specific custom template directory.
            Templates here override SDK defaults by matching filename.
        assets: Optional list of asset type names to filter (e.g., ["TABLE", "COLUMN"]).

    Returns:
        A dictionary mapping of data assets to the path of the YAML file that contains the SQL query template.
    """
    return _get_yaml_query_template_path_mappings(custom_templates_path, assets)


@deprecated(
    "application_sdk.transformers.common.utils.flatten_yaml_columns is deprecated; "
    "use application_sdk.common.yaml_templates.flatten_yaml_columns instead — "
    "will be removed in v4.0."
)
def flatten_yaml_columns(
    nested: dict, parent_key: str = "", sep: str = "."
) -> list[dict]:
    """Flatten a nested ``columns:`` mapping into a list of dotted-name columns.

    Args:
        nested (dict): The nested columns dictionary.
        parent_key (str): The prefix for the current level.
        sep (str): The separator to use (default: ".").

    Returns:
        list[dict]: A flat list of column definitions with dot notation.
    """
    return _flatten_yaml_columns(nested, parent_key, sep)
