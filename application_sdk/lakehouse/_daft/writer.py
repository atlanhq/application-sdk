"""Internal: write a Daft DataFrame to an Iceberg table.

Mirrors ``atlan-automation-engine-app``'s ``write_lakehouse`` activity:
load the target Iceberg table via PyIceberg, then call
``df.write_iceberg(iceberg_table, mode=…)``. Daft handles streaming + the
metadata commit; PyIceberg owns table identity and partitioning.

Apps interact via :meth:`LakehouseWriter.append_dataframe`. The Daft
DataFrame stays as the public boundary type — exposing Daft is acceptable
because it's a deliberately-chosen processing engine, unlike pyarrow /
pyiceberg which are *implementation* details we hide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pyiceberg.catalog import Catalog
from pyiceberg.exceptions import NoSuchTableError

from application_sdk.lakehouse._iceberg import identifier as _identifier
from application_sdk.lakehouse._iceberg import ops as _ops
from application_sdk.lakehouse.schema import Schema

if TYPE_CHECKING:
    import daft

WriteMode = Literal["append", "overwrite"]


def write_dataframe(
    catalog: Catalog,
    namespace: str,
    table_name: str,
    df: "daft.DataFrame",
    *,
    mode: WriteMode = "append",
    schema: Schema | None = None,
) -> int:
    """Write a Daft DataFrame to an Iceberg table. Returns the row count.

    If ``schema`` is provided and the table does not exist, the SDK creates
    it from the SDK :class:`Schema` (and its partition spec) before writing.
    """
    try:
        table = catalog.load_table(_identifier.identifier(namespace, table_name))
    except (NoSuchTableError, Exception):
        if schema is None:
            raise
        table = _ops.ensure_table(catalog, namespace, table_name, schema)

    result_df = df.write_iceberg(table, mode=mode)
    rows = result_df.to_pylist()
    return sum(int(row.get("num_rows", 0)) for row in rows)
