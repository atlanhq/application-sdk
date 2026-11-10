"""Arrow conversion shared by the SDK's Parquet writers.

Lives in ``common`` rather than ``storage.formats`` because code outside that
package (``common.file_converter``, the scale-data generator) writes Parquet
through it. It was the one thing tying them to ``storage.formats``, which is
headed for removal (FND-4693).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

__all__ = ["arrow_table_from_pandas"]


def arrow_table_from_pandas(
    dataframe: "pd.DataFrame", *, preserve_index: bool | None = False
) -> "pa.Table":
    """Convert a DataFrame to Arrow with the Parquet schema pandas 2 produced.

    pandas 3 infers text columns as its ``str`` dtype, which pyarrow converts
    to ``large_string``. Under pandas 2 the same column was ``object`` and
    converted to ``string``. Without this cast, upgrading pandas would change
    the type of every text column in every Parquet file the SDK writes, and
    files written before and after the upgrade would disagree in a shared
    output prefix. Only columns of the pandas 3 default ``str`` dtype (a
    ``StringDtype`` whose missing value is NaN) are cast; a column the caller
    typed explicitly, such as ``string[pyarrow]`` or an ``ArrowDtype``, keeps
    the type pyarrow gives it.

    A column whose text exceeds the 2 GiB ``string`` offset limit within one
    chunk cannot be cast; it stays ``large_string``. The readers merge shards
    with ``promote_options="permissive"``, which reconciles the two types.
    """
    import pandas as pd  # noqa: PLC0415 — optional dep: pandas
    import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

    table = pa.Table.from_pandas(dataframe, preserve_index=preserve_index)
    for name, dtype in dataframe.dtypes.items():
        if not isinstance(dtype, pd.StringDtype):
            continue
        # A StringDtype whose missing value is pd.NA ("string") was chosen by
        # the caller; only the NaN-valued one is pandas 3's inferred default.
        if dtype.na_value is pd.NA:
            continue
        index = table.schema.get_field_index(str(name))
        if index < 0 or not pa.types.is_large_string(table.schema.field(index).type):
            continue
        field = table.schema.field(index)
        try:
            column = table.column(index).cast(pa.string())
        except pa.ArrowInvalid:
            continue
        table = table.set_column(index, field.with_type(pa.string()), column)
    return table
