from unittest.mock import Mock, patch

import pandas as pd
import pytest
from pyiceberg.catalog import Catalog
from pyiceberg.table import Table

from application_sdk.common.types import DataframeType
from application_sdk.io.iceberg import IcebergTableWriter


@pytest.fixture
def mock_catalog() -> Catalog:
    return Mock(spec=Catalog)


@pytest.fixture
def mock_table() -> Table:
    return Mock(spec=Table)


@pytest.fixture
def iceberg_output(mock_catalog: Catalog) -> IcebergTableWriter:
    return IcebergTableWriter(
        iceberg_catalog=mock_catalog,
        iceberg_namespace="test_namespace",
        iceberg_table="test_table",
        mode="append",
        dataframe_type=DataframeType.pandas,
    )


def test_iceberg_output_initialization(mock_catalog: Catalog) -> None:
    """Test IcebergTableWriter initialization with different parameters"""
    output = IcebergTableWriter(
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
async def test_write_empty(iceberg_output: IcebergTableWriter) -> None:
    """Test writing empty dataframe"""
    df = pd.DataFrame()
    await iceberg_output.write(df)
    # Should return without doing anything for empty dataframe
    assert iceberg_output.total_record_count == 0
    assert iceberg_output.chunk_count == 0


@pytest.mark.asyncio
async def test_write_with_data(iceberg_output: IcebergTableWriter) -> None:
    """Test writing dataframe with data"""
    test_data = pd.DataFrame({"col1": [1, 2, 3], "col2": ["a", "b", "c"]})

    mock_daft_df = Mock()
    mock_daft_df.count_rows.return_value = 3

    with patch("daft.from_pandas") as mock_from_pandas:
        mock_from_pandas.return_value = mock_daft_df

        await iceberg_output.write(test_data)
        mock_from_pandas.assert_called_once_with(test_data)


@pytest.mark.asyncio
async def test_write_existing_table(
    iceberg_output: IcebergTableWriter, mock_table: Table
) -> None:
    """Test writing daft dataframe to existing table"""
    df = pd.DataFrame({"col1": [1, 2, 3, 4, 5]})
    iceberg_output.iceberg_table = mock_table

    with patch("daft.from_pandas") as mock_from_pandas:
        mock_daft_df = Mock()
        mock_daft_df.count_rows.return_value = 5
        mock_daft_df.write_iceberg.return_value = None
        mock_from_pandas.return_value = mock_daft_df

        await iceberg_output.write(df)

        assert iceberg_output.total_record_count == 5
        assert iceberg_output.chunk_count == 1
        mock_daft_df.write_iceberg.assert_called_once_with(mock_table, mode="append")


@pytest.mark.asyncio
async def test_write_new_table(
    iceberg_output: IcebergTableWriter, mock_table: Table
) -> None:
    """Test writing daft dataframe creating new table"""
    df = pd.DataFrame({"col1": [1, 2, 3]})
    mock_arrow_schema = Mock()

    iceberg_output.iceberg_catalog.create_table_if_not_exists.return_value = mock_table

    with patch("daft.from_pandas") as mock_from_pandas:
        mock_daft_df = Mock()
        mock_daft_df.count_rows.return_value = 3
        mock_daft_df.write_iceberg.return_value = None
        mock_daft_df.to_arrow.return_value.schema = mock_arrow_schema
        mock_from_pandas.return_value = mock_daft_df

        await iceberg_output.write(df)

        iceberg_output.iceberg_catalog.create_table_if_not_exists.assert_called_once_with(
            "test_namespace.test_table", schema=mock_arrow_schema
        )
        assert iceberg_output.total_record_count == 3
        assert iceberg_output.chunk_count == 1


@pytest.mark.asyncio
async def test_write_error_handling(iceberg_output: IcebergTableWriter) -> None:
    """Test error handling in write"""
    df = pd.DataFrame({"col1": [1, 2, 3]})

    with patch("daft.from_pandas") as mock_from_pandas:
        mock_from_pandas.side_effect = Exception("Test error")

        with pytest.raises(Exception, match="Test error"):
            await iceberg_output.write(df)
        # Verify counts remain unchanged
        assert iceberg_output.total_record_count == 0
        assert iceberg_output.chunk_count == 0


class TestIcebergTableWriterDict:
    """Test cases for IcebergTableWriter dictionary handling."""

    @pytest.mark.asyncio
    async def test_write_single_dict(
        self, mock_catalog: Catalog, mock_table: Table
    ) -> None:
        """Test writing a single dictionary to Iceberg table."""
        iceberg_output = IcebergTableWriter(
            iceberg_catalog=mock_catalog,
            iceberg_namespace="test_namespace",
            iceberg_table=mock_table,
            mode="append",
            dataframe_type=DataframeType.dict,
        )

        mock_daft_df = Mock()
        mock_daft_df.count_rows.return_value = 1

        with patch("daft.from_pandas") as mock_from_pandas:
            mock_from_pandas.return_value = mock_daft_df
            mock_daft_df.write_iceberg.return_value = None

            data = {"col1": 1, "col2": "a"}
            await iceberg_output.write(data)

            assert iceberg_output.total_record_count == 1
            assert iceberg_output.chunk_count == 1
            mock_from_pandas.assert_called_once()
            mock_daft_df.write_iceberg.assert_called_once_with(
                mock_table, mode="append"
            )

    @pytest.mark.asyncio
    async def test_write_list_of_dicts(
        self, mock_catalog: Catalog, mock_table: Table
    ) -> None:
        """Test writing a list of dictionaries to Iceberg table."""
        iceberg_output = IcebergTableWriter(
            iceberg_catalog=mock_catalog,
            iceberg_namespace="test_namespace",
            iceberg_table=mock_table,
            mode="append",
            dataframe_type=DataframeType.dict,
        )

        mock_daft_df = Mock()
        mock_daft_df.count_rows.return_value = 2

        with patch("daft.from_pandas") as mock_from_pandas:
            mock_from_pandas.return_value = mock_daft_df
            mock_daft_df.write_iceberg.return_value = None

            data = [{"col1": 1, "col2": "a"}, {"col1": 2, "col2": "b"}]
            await iceberg_output.write(data)

            assert iceberg_output.total_record_count == 2
            assert iceberg_output.chunk_count == 1
            mock_from_pandas.assert_called_once()
            mock_daft_df.write_iceberg.assert_called_once_with(
                mock_table, mode="append"
            )

    @pytest.mark.asyncio
    async def test_write_empty_dict(
        self, mock_catalog: Catalog, mock_table: Table
    ) -> None:
        """Test writing an empty dictionary list to Iceberg table."""
        iceberg_output = IcebergTableWriter(
            iceberg_catalog=mock_catalog,
            iceberg_namespace="test_namespace",
            iceberg_table=mock_table,
            mode="append",
            dataframe_type=DataframeType.dict,
        )

        data: list = []
        await iceberg_output.write(data)

        # Should return without doing anything for empty list
        assert iceberg_output.total_record_count == 0
        assert iceberg_output.chunk_count == 0

    @pytest.mark.asyncio
    async def test_write_batches_dicts(
        self, mock_catalog: Catalog, mock_table: Table
    ) -> None:
        """Test writing batches of dictionaries to Iceberg table."""
        iceberg_output = IcebergTableWriter(
            iceberg_catalog=mock_catalog,
            iceberg_namespace="test_namespace",
            iceberg_table=mock_table,
            mode="append",
            dataframe_type=DataframeType.dict,
        )

        # Create separate mocks for each batch with correct row counts
        mock_daft_df_1 = Mock()
        mock_daft_df_1.count_rows.return_value = 1
        mock_daft_df_2 = Mock()
        mock_daft_df_2.count_rows.return_value = 2

        with patch("daft.from_pandas") as mock_from_pandas:
            # Return different mocks for each call
            mock_from_pandas.side_effect = [mock_daft_df_1, mock_daft_df_2]
            mock_daft_df_1.write_iceberg.return_value = None
            mock_daft_df_2.write_iceberg.return_value = None

            def dict_generator():
                yield {"id": 1}
                yield [{"id": 2}, {"id": 3}]

            await iceberg_output.write_batches(dict_generator())

            # Should have written 3 records total (1 + 2)
            assert iceberg_output.total_record_count == 3
            assert iceberg_output.chunk_count == 2  # Two batches
            assert mock_from_pandas.call_count == 2

    @pytest.mark.asyncio
    async def test_write_invalid_dict_input(
        self, mock_catalog: Catalog, mock_table: Table
    ) -> None:
        """Test writing invalid dictionary input raises error."""
        iceberg_output = IcebergTableWriter(
            iceberg_catalog=mock_catalog,
            iceberg_namespace="test_namespace",
            iceberg_table=mock_table,
            mode="append",
            dataframe_type=DataframeType.dict,
        )

        invalid_data = "not_a_dict"
        with pytest.raises(ValueError, match="Invalid dictionary input"):
            await iceberg_output.write(invalid_data)  # type: ignore
