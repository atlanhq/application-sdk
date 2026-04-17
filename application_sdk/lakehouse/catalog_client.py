"""Polaris catalog client for snapshot change detection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from pyiceberg.table import Table

logger = logging.getLogger(__name__)


class PolarisCatalogClient:
    """Thin wrapper for snapshot change detection. Each has_changed() call
    issues one GET to Polaris REST catalog. No S3/ADLS access needed."""

    def __init__(self, catalog: "Catalog", namespace: str, table_name: str) -> None:
        self._catalog = catalog
        self._identifier = (namespace, table_name)

    def load_table(self) -> "Table":
        return self._catalog.load_table(self._identifier)

    def get_current_snapshot_id(self) -> int | None:
        table = self.load_table()
        snapshot = table.current_snapshot()
        if snapshot is None:
            return None
        return snapshot.snapshot_id

    def has_changed(self, last_snapshot_id: int | None) -> bool:
        current = self.get_current_snapshot_id()
        if current is None:
            return False
        return current != last_snapshot_id
