"""Declarative entity definitions for metadata extraction.

An ``EntityDef`` describes a single entity type to extract (e.g. databases,
schemas, tables, columns, stages, streams).  Templates like
``SqlMetadataExtractor`` use a list of these to orchestrate extraction
automatically — no need to override ``run()``.

Example::

    class SnowflakeExtractor(SqlMetadataExtractor):
        entities = [
            EntityDef(name="databases", phase=1),
            EntityDef(name="schemas",   phase=1),
            EntityDef(name="tables",    phase=1),
            EntityDef(name="columns",   phase=1),
            EntityDef(name="stages",    sql="SELECT ... FROM STAGES", phase=2),
            EntityDef(name="streams",   sql="SELECT ... FROM STREAMS", phase=2),
        ]
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntityDef:
    """Declares an extractable entity type.

    Protocol-agnostic: works for SQL connectors (set ``sql``),
    REST API connectors (set ``endpoint``), or custom logic
    (leave both empty, implement ``fetch_{name}()`` method).
    """

    name: str
    """Entity name used in output keys, logging, and method dispatch
    (e.g. ``'databases'`` → ``fetch_databases()``)."""

    # --- Fetch strategy (exactly one should be set, or none for custom method) ---

    sql: str = ""
    """SQL query template for SQL connectors.  Supports
    ``{normalized_include_regex}``, ``{normalized_exclude_regex}``,
    ``{temp_table_regex_sql}``, ``{database_name}`` placeholders."""

    endpoint: str = ""
    """REST API endpoint path for API connectors (e.g. ``'/api/3.1/workbooks'``).
    Relative to the client's ``BASE_URL``."""

    pagination: str = "none"
    """Pagination strategy for API endpoints:
    ``'none'``, ``'cursor'``, ``'offset'``, ``'page_token'``.
    Ignored for SQL entities."""

    response_items_key: str = ""
    """JSON key containing the array of items in API response
    (e.g. ``'workbooks.workbook'``).  Empty = response is the array.
    Ignored for SQL entities."""

    # --- Orchestration ---

    phase: int = 1
    """Execution phase (1 = parallel with core entities, 2 = after phase 1, etc.).
    Entities in the same phase run concurrently."""

    depends_on: tuple[str, ...] = ()
    """Entity names that must complete before this one starts.
    Empty = runs in the first parallel batch of its phase.
    Not yet implemented in orchestration — reserved for future use.

    Example::

        entities = [
            EntityDef(name="databases", phase=1),
            EntityDef(name="schemas",   phase=1),
            # stages waits only for databases, not all of phase 1
            EntityDef(name="stages",  phase=2, depends_on=("databases",)),
            # streams waits for both databases and schemas
            EntityDef(name="streams", phase=2, depends_on=("databases", "schemas")),
        ]
    """

    enabled: bool = True
    """Set to False to skip this entity (e.g. exclude_views toggle)."""

    timeout_seconds: int = 1800
    """Task timeout for this entity's extraction."""

    # --- Output ---

    result_key: str = ""
    """Key in ExtractionOutput for the count.
    Defaults to ``'{name}_extracted'`` if empty."""
