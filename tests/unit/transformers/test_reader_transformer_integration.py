"""Regression tests for the ParquetFileReader -> transformer seam.

`ParquetFileReader.read()` returns a pandas DataFrame (see
`tests/unit/storage/formats/readers/test_parquet_reader.py`), but historically
neither `QueryBasedTransformer.transform_metadata` nor
`AtlasTransformer.transform_metadata` accepted anything but a `pyarrow.Table`
or `list[dict]`. Feeding a reader's real output straight into a transformer —
the standard reader -> transformer usage pattern — raised an ``AttributeError``.

This file drives that exact path end to end (a real parquet file on disk,
read through the real reader, fed into the real transformer) rather than
mocking either side, since unit tests on each side in isolation never
exercised the seam between them.
"""

from pathlib import Path
from unittest.mock import mock_open, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from application_sdk.common.types import DataframeType
from application_sdk.storage.formats.parquet import ParquetFileReader
from application_sdk.transformers.atlas import AtlasTransformer
from application_sdk.transformers.query import QueryBasedTransformer


def _make_parquet_file(tmp_path: Path) -> str:
    table = pa.Table.from_pydict(
        {
            "table_name": ["table1", "table2"],
            "table_catalog": ["db1", "db2"],
            "table_schema": ["schema1", "schema2"],
        }
    )
    path = tmp_path / "data.parquet"
    pq.write_table(table, path)
    return str(path)


async def _read_via_reader(tmp_path: Path):
    """Read a parquet file the same way a real activity would."""
    parquet_file = _make_parquet_file(tmp_path)

    async def dummy_download(path, file_extension, file_names=None):
        return [parquet_file]

    with patch(
        "application_sdk.storage.formats.parquet._download_files",
        side_effect=dummy_download,
    ):
        reader = ParquetFileReader(
            path=str(tmp_path), dataframe_type=DataframeType.pandas
        )
        return await reader.read()


@pytest.mark.asyncio
async def test_reader_output_feeds_atlas_transformer(tmp_path: Path) -> None:
    """AtlasTransformer.transform_metadata must accept a real reader's output."""
    dataframe = await _read_via_reader(tmp_path)

    transformer = AtlasTransformer(
        connector_name="test_connector", tenant_id="test_tenant"
    )

    # Must not raise AttributeError('DataFrame' object has no attribute 'to_pylist').
    result = transformer.transform_metadata(
        "TABLE",
        dataframe,
        "test_workflow",
        "test_run",
        connection={
            "connection_qualified_name": "default/postgres/123",
            "connection_name": "test_conn",
        },
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_reader_output_feeds_query_based_transformer(tmp_path: Path) -> None:
    """QueryBasedTransformer.transform_metadata must accept a real reader's output."""
    dataframe = await _read_via_reader(tmp_path)

    transformer = QueryBasedTransformer(
        connector_name="test_connector", tenant_id="test_tenant"
    )
    transformer.entity_class_definitions = {"TABLE": "dummy_path.yaml"}

    sql_template = {
        "columns": {
            "attributes": {
                "name": {"source_query": "table_name"},
            }
        }
    }

    with (
        patch("builtins.open", mock_open()),
        patch("yaml.safe_load", return_value=sql_template),
    ):
        # Must not raise AttributeError('DataFrame' object has no attribute 'schema').
        result = transformer.transform_metadata(
            "TABLE",
            dataframe,
            "test_workflow",
            "test_run",
            connection_qualified_name="default/postgres/123",
            connection_name="test_conn",
        )

    assert result is not None
    assert len(result) == 2
    assert {row["attributes"]["name"] for row in result} == {"table1", "table2"}
