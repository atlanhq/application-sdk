from unittest.mock import Mock, patch

import pandas as pd
import pytest
from pyiceberg.catalog import Catalog
from pyiceberg.table import Table

from application_sdk.outputs.iceberg import IcebergOutput


@pytest.fixture
def mock_catalog() -> Catalog:
    return Mock(spec=Catalog)


@pytest.fixture
def mock_table() -> Table:
    return Mock(spec=Table)


@pytest.fixture
def iceberg_output(mock_catalog: Catalog) -> IcebergOutput:
    return IcebergOutput(
        iceberg_catalog=mock_catalog,
        iceberg_namespace="test_namespace",
        iceberg_table="test_table",
        mode="append",
    )


def test_iceberg_output_initialization(mock_catalog: Catalog) -> None:
    """Test IcebergOutput initialization with different parameters"""
    output = IcebergOutput(
        iceberg_catalog=mock_catalog,
        iceberg_namespace="test_namespace",
        iceberg_table="test_table",
        mode="append",
    )

    assert output.iceberg_catalog == mock_catalog
    assert output.iceberg_namespace == "test_namespace"
    assert output.iceberg_table == "test_table"
    assert output.mode == "append"
    assert output.total_record_count == 0
    assert output.chunk_count == 0


@pytest.mark.asyncio
async def test_write_dataframe_empty(iceberg_output: IcebergOutput) -> None:
    """Test writing empty dataframe"""
    df = pd.DataFrame()
    await iceberg_output.write_dataframe(df)
    # Should return without doing anything for empty dataframe
    assert iceberg_output.total_record_count == 0
    assert iceberg_output.chunk_count == 0


@pytest.mark.asyncio
async def test_write_dataframe_with_data(iceberg_output: IcebergOutput) -> None:
    """Test writing dataframe with data"""
    test_data = pd.DataFrame({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})

    mock_daft_df = Mock()
    mock_daft_df.count_rows.return_value = 3

    with patch("daft.from_pandas") as mock_from_pandas:
        mock_from_pandas.return_value = mock_daft_df

        await iceberg_output.write_dataframe(test_data)
        mock_from_pandas.assert_called_once_with(test_data)


@pytest.mark.asyncio
async def test_write_daft_dataframe_existing_table(
    iceberg_output: IcebergOutput, mock_table: Table
) -> None:
    """Test writing daft dataframe to existing table"""
    mock_df = Mock()
    mock_df.count_rows.return_value = 5
    iceberg_output.iceberg_table = mock_table

    await iceberg_output.write_daft_dataframe(mock_df)

    assert iceberg_output.total_record_count == 5
    assert iceberg_output.chunk_count == 1
    mock_df.write_iceberg.assert_called_once_with(mock_table, mode="append")


@pytest.mark.asyncio
async def test_write_daft_dataframe_new_table(
    iceberg_output: IcebergOutput, mock_table: Table
) -> None:
    """Test writing daft dataframe creating new table"""
    mock_df = Mock()
    mock_df.count_rows.return_value = 3
    mock_arrow_schema = Mock()
    mock_df.to_arrow.return_value.schema = mock_arrow_schema

    iceberg_output.iceberg_catalog.create_table_if_not_exists.return_value = mock_table

    await iceberg_output.write_daft_dataframe(mock_df)

    iceberg_output.iceberg_catalog.create_table_if_not_exists.assert_called_once_with(
        "test_namespace.test_table", schema=mock_arrow_schema
    )
    assert iceberg_output.total_record_count == 3
    assert iceberg_output.chunk_count == 1
    mock_df.write_iceberg.assert_called_once_with(mock_table, mode="append")


@pytest.mark.asyncio
async def test_write_dataframe_error_handling(iceberg_output: IcebergOutput) -> None:
    """Test error handling in write_dataframe"""
    df = pd.DataFrame({"col1": [1, 2, 3]})

    with patch("daft.from_pandas") as mock_from_pandas:
        mock_from_pandas.side_effect = Exception("Test error")

        with pytest.raises(Exception, match="Test error"):
            await iceberg_output.write_dataframe(df)
        # Verify counts remain unchanged
        assert iceberg_output.total_record_count == 0
        assert iceberg_output.chunk_count == 0


@pytest.mark.asyncio
async def test_write_daft_dataframe_error_handling(
    iceberg_output: IcebergOutput,
) -> None:
    """Test error handling in write_daft_dataframe"""
    mock_df = Mock()
    mock_df.count_rows.side_effect = Exception("Test error")

    with pytest.raises(Exception, match="Test error"):
        await iceberg_output.write_daft_dataframe(mock_df)
    # Verify counts remain unchanged
    assert iceberg_output.total_record_count == 0
    assert iceberg_output.chunk_count == 0
