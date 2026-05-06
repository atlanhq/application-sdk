"""Internal: bulk-write Parquet-staged data to an Iceberg table via Daft.

Apps stage their large dataset as Parquet files (anywhere Daft can read —
local filesystem, S3, GCS, ADLS) and pass the prefix path. This module reads
those files lazily into a Daft DataFrame and commits them as a single Iceberg
snapshot via ``df.write_iceberg``.

Daft is an implementation detail; the public surface
(``LakehouseWriter.append_bulk``) speaks only in ``str`` paths and
``int`` row counts. Mirrors automation-engine-app's ``write_lakehouse``
activity pattern.
"""

from __future__ import annotations

from typing import Literal

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError

from application_sdk.lakehouse._iceberg import identifier as _identifier
from application_sdk.lakehouse._iceberg import ops as _ops
from application_sdk.lakehouse.schema import Schema

WriteMode = Literal["append", "overwrite"]


def _import_daft():
    """Defer the daft import so apps without [lakehouse-bulk] don't ImportError."""
    try:
        import daft  # noqa: PLC0415

        return daft
    except ImportError as exc:
        raise ImportError(
            "daft is required for bulk writes. "
            "Install with: pip install atlan-application-sdk[lakehouse-bulk]"
        ) from exc


def write_bulk(
    catalog: Catalog,
    namespace: str,
    table_name: str,
    source_prefix: str,
    *,
    mode: WriteMode = "append",
    schema: Schema | None = None,
) -> int:
    """Read Parquet files at ``source_prefix`` and commit them to an Iceberg table.

    If ``schema`` is provided and the table does not exist, the SDK creates
    it from the SDK :class:`Schema` (and its partition spec) before writing.
    """
    daft = _import_daft()

    try:
        table = catalog.load_table(_identifier.identifier(namespace, table_name))
    except (NoSuchTableError, Exception):
        if schema is None:
            raise
        table = _ops.ensure_table(catalog, namespace, table_name, schema)

    df = daft.read_parquet(source_prefix)
    result_df = df.write_iceberg(table, mode=mode)
    rows = result_df.to_pylist()
    return sum(int(row.get("num_rows", 0)) for row in rows)
