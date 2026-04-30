"""Generic lakehouse writer bound to an app's own namespace.

Each app owns one namespace. The writer is constructed against that namespace
and any append targeting a different namespace logs a warning but still
proceeds — catalog RBAC is the authoritative enforcement; the warning surfaces
the violation in app logs so it's caught in code review.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
from pyiceberg.partitioning import PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table

from application_sdk.lakehouse.catalog_client import CatalogClient

logger = logging.getLogger(__name__)


class LakehouseWriter:
    """Append-only writer bound to a single ``app_namespace``."""

    def __init__(self, catalog_client: CatalogClient, app_namespace: str) -> None:
        self._client = catalog_client
        self._app_namespace = app_namespace

    @classmethod
    def from_env(cls, app_namespace: str) -> LakehouseWriter:
        """Build a writer using the catalog credentials from the environment.

        See :func:`application_sdk.lakehouse.catalog_client.load_catalog_from_env`
        for the env vars consumed.
        """
        return cls(CatalogClient.from_env(), app_namespace)

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
        arrow_table: pa.Table,
        namespace: str | None = None,
    ) -> int:
        """Append an arrow table to an existing Iceberg table. Returns row count.

        ``namespace`` defaults to the writer's bound ``app_namespace``. Passing
        a different namespace is allowed but logged as a warning.
        """
        target_namespace = namespace or self._app_namespace
        self._check_namespace(target_namespace)
        if arrow_table.num_rows == 0:
            return 0
        table = self._client.load_table(target_namespace, table_name)
        table.append(arrow_table)
        return arrow_table.num_rows

    def create_or_get_table(
        self,
        table_name: str,
        schema: Schema,
        partition_spec: PartitionSpec | None = None,
        namespace: str | None = None,
    ) -> Table:
        """Load the table, creating it (and its namespace) if missing."""
        target_namespace = namespace or self._app_namespace
        self._check_namespace(target_namespace)
        try:
            return self._client.load_table(target_namespace, table_name)
        except Exception:
            self._client.ensure_namespace(target_namespace)
            identifier = CatalogClient.identifier(target_namespace, table_name)
            kwargs: dict[str, Any] = {"identifier": identifier, "schema": schema}
            if partition_spec is not None:
                kwargs["partition_spec"] = partition_spec
            return self._client.catalog.create_table(**kwargs)

    def write_records(
        self,
        table_name: str,
        records: list[dict[str, Any]],
        schema: Schema,
        partition_spec: PartitionSpec | None = None,
        arrow_schema: pa.Schema | None = None,
        namespace: str | None = None,
    ) -> int:
        """Create-or-get the table, then append records. Returns row count.

        Builds the arrow table using ``arrow_schema`` if provided, otherwise
        falls back to ``schema.as_arrow()`` from the iceberg schema.
        """
        if not records:
            return 0
        target_namespace = namespace or self._app_namespace
        table = self.create_or_get_table(
            table_name, schema, partition_spec, namespace=target_namespace
        )
        effective_arrow_schema = arrow_schema or schema.as_arrow()
        arrow_table = pa.Table.from_pylist(records, schema=effective_arrow_schema)
        table.append(arrow_table)
        return arrow_table.num_rows
