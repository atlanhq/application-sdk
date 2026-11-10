"""``common._arrow.arrow_table_from_pandas``: Parquet text columns stay ``string``."""

import pandas as pd
import pyarrow as pa
import pytest

from application_sdk.common._arrow import arrow_table_from_pandas


def test_default_text_column_is_written_as_string() -> None:
    """A text column inferred by pandas converts to Arrow ``string``.

    Under pandas 3 it is the ``str`` dtype, which pyarrow converts to
    ``large_string``; under pandas 2 it is ``object``, which converts to
    ``string``. The SDK's Parquet files keep ``string`` either way.
    """
    dataframe = pd.DataFrame({"id": [1, 2, 3], "name": ["a", None, "c"]})

    table = arrow_table_from_pandas(dataframe)

    assert table.schema.field("name").type == pa.string()
    assert table.column("name").to_pylist() == ["a", None, "c"]
    assert table.schema.field("id").type == pa.int64()


def test_explicit_str_dtype_is_written_as_string() -> None:
    dataframe = pd.DataFrame({"name": pd.Series(["a", "b"], dtype="str")})

    table = arrow_table_from_pandas(dataframe)

    assert table.schema.field("name").type == pa.string()


def test_caller_typed_arrow_column_keeps_its_type() -> None:
    """A column typed by the caller is not the inferred default, so it is not cast."""
    dataframe = pd.DataFrame(
        {"name": pd.Series(["a", "b"], dtype=pd.ArrowDtype(pa.large_string()))}
    )

    table = arrow_table_from_pandas(dataframe)

    assert table.schema.field("name").type == pa.large_string()


def test_caller_typed_string_dtype_keeps_its_type() -> None:
    """``dtype="string"`` (missing value ``pd.NA``) is the caller's choice, not
    the pandas 3 inferred default, so it converts exactly as pyarrow converts it.
    """
    dataframe = pd.DataFrame({"name": pd.Series(["a", None], dtype="string")})

    table = arrow_table_from_pandas(dataframe)
    expected = pa.Table.from_pandas(dataframe, preserve_index=False)

    assert table.schema.field("name").type == expected.schema.field("name").type
    assert table.column("name").to_pylist() == ["a", None]


@pytest.mark.parametrize("preserve_index", [False, None])
def test_index_handling_matches_from_pandas(preserve_index: bool | None) -> None:
    dataframe = pd.DataFrame({"name": ["a", "b"]}, index=[5, 6])

    table = arrow_table_from_pandas(dataframe, preserve_index=preserve_index)
    expected = pa.Table.from_pandas(dataframe, preserve_index=preserve_index)

    assert table.schema.names == expected.schema.names
