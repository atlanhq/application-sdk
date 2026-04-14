"""Declarative entity definitions for metadata extraction.

An ``ExtractableEntity`` describes a single entity type to extract
(e.g. databases, schemas, tables, columns, stages, streams).  Templates
like ``SqlMetadataExtractor`` use a list of these to orchestrate
extraction automatically — no need to override ``run()``.

Each entity maps **explicitly** to a task method via ``task_name`` —
no naming-convention magic.

Example::

    class SnowflakeExtractor(SqlMetadataExtractor):
        entities = [
            ExtractableEntity(task_name="fetch_databases", phase=1),
            ExtractableEntity(task_name="fetch_schemas",   phase=1),
            ExtractableEntity(task_name="fetch_tables",    phase=1),
            ExtractableEntity(task_name="fetch_columns",   phase=1),
            ExtractableEntity(task_name="fetch_stages",    phase=2),
            ExtractableEntity(task_name="fetch_streams",   phase=2),
        ]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ExtractableEntity:
    """Declares an extractable entity type.

    Each entity maps explicitly to a method on the App subclass via
    ``task_name``.  No naming conventions — the entity tells the
    orchestrator exactly which method to call.
    """

    task_name: str
    """The exact method name on the App subclass to call for this entity
    (e.g. ``'fetch_databases'``, ``'fetch_stages'``)."""

    # --- Orchestration ---

    phase: int = 1
    """Execution phase (1 = first, 2 = after phase 1 completes, etc.).
    Entities in the same phase run concurrently."""

    depends_on: tuple[str, ...] = ()
    """Task names that must complete before this one starts.
    Not yet implemented in orchestration — reserved for future use.

    Example::

        entities = [
            ExtractableEntity(task_name="fetch_databases", phase=1),
            ExtractableEntity(task_name="fetch_schemas",   phase=1),
            ExtractableEntity(task_name="fetch_stages",  phase=2,
                              depends_on=("fetch_databases",)),
        ]
    """

    enabled: bool = True
    """Set to False to skip this entity (e.g. exclude_views toggle)."""

    timeout_seconds: int = 1800
    """Task timeout for this entity's extraction."""

    # --- Output ---

    result_key: str = ""
    """Key in the output model for the count.
    Defaults to ``'{base}_extracted'`` where *base* is ``task_name``
    with the ``fetch_`` prefix stripped
    (e.g. ``'fetch_databases'`` -> ``'databases_extracted'``)."""


def default_result_key(task_name: str) -> str:
    """Derive a default result key from a task name.

    ``'fetch_databases'`` -> ``'databases_extracted'``
    ``'fetch_stages'``    -> ``'stages_extracted'``
    """
    base = task_name.removeprefix("fetch_")
    return f"{base}_extracted"


async def run_entity_phases(
    app: Any,
    entities: list[ExtractableEntity],
    base_input: Any,
) -> dict[str, int]:
    """Execute entities grouped by phase, returning ``{result_key: count}``.

    Entities in the same phase run concurrently via ``asyncio.gather``.
    If any entity in a phase fails, sibling entities in that phase still
    run to completion before the first error is re-raised
    (``return_exceptions=True``).

    The *app* must implement ``_fetch_entity(entity, base_input)``
    returning ``(result_key, count)``.
    """
    phases: dict[int, list[ExtractableEntity]] = {}
    for entity in entities:
        phases.setdefault(entity.phase, []).append(entity)

    results: dict[str, int] = {}
    for phase_num in sorted(phases):
        phase_entities = phases[phase_num]
        phase_results = await asyncio.gather(
            *[app._fetch_entity(entity, base_input) for entity in phase_entities],
            return_exceptions=True,
        )

        errors: list[tuple[ExtractableEntity, BaseException]] = []
        for entity, result in zip(phase_entities, phase_results):
            if isinstance(result, BaseException):
                errors.append((entity, result))
            else:
                result_key, count = result
                results[result_key] = count

        if errors:
            for entity, err in errors[1:]:
                logger.error(
                    "Entity '%s' also failed in phase %d: %s",
                    entity.task_name,
                    phase_num,
                    err,
                )
            raise errors[0][1]

    return results
