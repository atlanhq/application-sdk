"""YAML transform-template helpers for apps that apply their own templates.

Re-homed from :mod:`application_sdk.transformers.common.utils` (FND-4690). That
module is removed in v4.0 with the rest of ``application_sdk.transformers``,
but most of the fleet's use of it never touched the deprecated transformer:
apps resolve their *own* YAML templates and run them through an app-side
applier. These two helpers are what they need from the SDK for that.

:func:`yaml_template_paths` differs from the function it replaces in one way,
on purpose: it resolves only the directory it is given. The old helper also
merged in the templates bundled with ``QueryBasedTransformer``
(``transformers/query/templates``). Those are SQL-connector templates for the
deprecated transformer and leave with it, so a helper in a non-deprecated
package cannot promise them. An app that relied on one copies it into its own
template directory.
"""

from __future__ import annotations

import glob
import os
from typing import Any

__all__ = ["flatten_yaml_columns", "yaml_template_paths"]


def yaml_template_paths(
    templates_path: str,
    assets: list[str] | None = None,
) -> dict[str, str]:
    """Map each YAML template under *templates_path* to its file path.

    The key is the file's basename, upper-cased, without the ``.yaml``
    extension (``tables/table.yaml`` → ``"TABLE"``). Subdirectories are
    searched recursively; when two files share a basename, the one that sorts
    last by path wins, so the result does not depend on filesystem order.

    Args:
        templates_path: Directory holding the app's YAML templates.
        assets: When given, keep only these keys (e.g. ``["TABLE", "COLUMN"]``).

    Returns:
        Template key → absolute or ``templates_path``-relative file path, as
        :func:`glob.glob` returns it.
    """
    files = sorted(
        glob.glob(os.path.join(templates_path, "**", "*.yaml"), recursive=True)
    )
    wanted = set(assets) if assets else None
    result: dict[str, str] = {}
    for file in files:
        key = os.path.basename(file).upper().removesuffix(".YAML")
        if wanted is None or key in wanted:
            result[key] = file
    return result


def flatten_yaml_columns(
    nested: dict[str, Any], parent_key: str = "", sep: str = "."
) -> list[dict[str, Any]]:
    """Flatten a template's nested ``columns:`` mapping into a list of columns.

    A key is joined to its parent with *sep* only when it sits under a non-leaf
    parent; a leaf is a scalar (taken as its ``source_query``) or a dict whose
    values are all scalars (taken as the column definition).

    Args:
        nested: The ``columns:`` mapping from a parsed template.
        parent_key: Prefix for the current level (used by the recursion).
        sep: Separator between nesting levels.

    Returns:
        One dict per leaf column, each with its dotted ``name``.

    Example:
        >>> flatten_yaml_columns(
        ...     {"attributes": {"name": {"source_query": "n"}}, "typeName": "'T'"}
        ... )
        [{'source_query': 'n', 'name': 'attributes.name'}, {'source_query': "'T'", 'name': 'typeName'}]
    """
    flat_columns: list[dict[str, Any]] = []
    for key, value in nested.items():
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict) and any(isinstance(v, dict) for v in value.values()):
            flat_columns.extend(flatten_yaml_columns(value, new_key, sep=sep))
        else:
            col_def = (
                value.copy() if isinstance(value, dict) else {"source_query": value}
            )
            col_def["name"] = new_key
            flat_columns.append(col_def)
    return flat_columns
