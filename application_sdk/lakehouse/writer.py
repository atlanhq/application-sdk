"""Generic lakehouse writer bound to an app's own namespace.

Each app owns one namespace. The writer is constructed against that namespace
and any append targeting a different namespace logs a warning but still
proceeds — catalog RBAC is the authoritative enforcement; the warning surfaces
the violation in app logs so it's caught in code review.

Apps interact with plain ``dict`` records and SDK :class:`Schema` declarations;
no pyiceberg or pyarrow types appear on the public surface.
"""

from __future__ import annotations

import logging
from typing import Any

from application_sdk.lakehouse._iceberg import catalog as _catalog
from application_sdk.lakehouse._iceberg import ops as _ops
from application_sdk.lakehouse.schema import Schema

logger = logging.getLogger(__name__)


class LakehouseWriter:
    """Append-only writer bound to a single ``app_namespace``."""

    def __init__(self, _catalog_obj: Any, app_namespace: str) -> None:
        self._catalog = _catalog_obj
        self._app_namespace = app_namespace

    @classmethod
    def from_env(cls, app_namespace: str) -> LakehouseWriter:
        """Build a writer from environment credentials, bound to ``app_namespace``.

        See :func:`application_sdk.lakehouse._iceberg.catalog.load_catalog_from_env`
        for the env vars consumed.
        """
        return cls(_catalog.load_catalog_from_env(), app_namespace)

    @property
    def app_namespace(self) -> str:
        return self._app_namespace

    def _check_namespace(self, target_namespace: str) -> None:
        if target_namespace != self._app_namespace:
            logger.warning(
                "Cross-namespace write: app_namespace=%s target=%s — apps should write "
                "only to their own namespace",
                self._app_namespace,
                target_namespace,
            )

    def append(
        self,
        table_name: str,
        records: list[dict[str, Any]],
        *,
        schema: Schema | None = None,
        namespace: str | None = None,
    ) -> int:
        """Append records to a table. Returns the number of rows appended.

        If ``schema`` is provided and the table does not exist, it is
        auto-created (and its namespace too) using that schema. If ``schema``
        is omitted the table must already exist; the writer infers the Arrow
        schema from the table's own metadata.

        ``namespace`` defaults to the writer's bound ``app_namespace``. Passing
        a different namespace is allowed but logged as a warning.
        """
        if not records:
            return 0
        target_namespace = namespace or self._app_namespace
        self._check_namespace(target_namespace)
        return _ops.append_records(
            self._catalog,
            target_namespace,
            table_name,
            records,
            schema=schema,
        )

    def ensure_table(
        self,
        table_name: str,
        schema: Schema,
        *,
        namespace: str | None = None,
    ) -> None:
        """Create the table from the SDK schema if it doesn't exist; no-op otherwise.

        Useful for migration steps that want to provision the table up-front
        without writing any rows.
        """
        target_namespace = namespace or self._app_namespace
        self._check_namespace(target_namespace)
        _ops.ensure_table(self._catalog, target_namespace, table_name, schema)
