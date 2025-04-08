import os
from concurrent.futures import Future
from typing import AsyncIterator
from unittest.mock import patch

import daft
import sqlalchemy
from pyiceberg.catalog.sql import SqlCatalog
from sqlalchemy.sql import text

from application_sdk.decorators import transform_daft
from application_sdk.inputs.iceberg import IcebergInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.iceberg import IcebergOutput

INSERT_QUERY = "INSERT INTO numbers (value) VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)"


def add_1(dataframe):
    """
    Similar to a transformation function that adds 1 to the value column
    """
    dataframe = dataframe.select(daft.col("value") + 1)
    return dataframe


class MockSingleThreadExecutor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def submit(self, fn, *args, **kwargs):
        future = Future()
        try:
            # Execute the function synchronously
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        return future


class TestDaftDecoratorsIceberg:
    @classmethod
    def setup_class(cls):
        """
        Method to setup the test clients
        """
        sqlite_db_url = "sqlite:////tmp/test_iceberg.db"
        cls.engine = sqlalchemy.create_engine(sqlite_db_url)
        warehouse_path = "/tmp/tests/warehouse"
        os.makedirs(warehouse_path, exist_ok=True)
        cls.namespace = "default"
        cls.catalog = SqlCatalog(
            "default",
            **{
                "uri": f"sqlite:///{warehouse_path}/pyiceberg_catalog.db",
                "warehouse": f"file://{warehouse_path}",
            },
        )
        cls.catalog.create_namespace("default")

    @classmethod
    def teardown_class(cls):
        """
        Clean up the test clients
        """
        cls.catalog.drop_table("default.test_table")
        cls.catalog.drop_table("default.test_table_two")
        cls.catalog.drop_table("default.test_table_three")
        cls.catalog.drop_table("default.test_table_four")
        cls.catalog.drop_namespace("default")

    def _create_test_clients(self, query: str):
        """
        Create the tables required for tests
        """
        with self.engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(text(query))
            conn.commit()

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_basic(self, _):
        """
        Basic test to read the SQL data
        """

        @transform_daft(
            batch_input=SQLQueryInput(engine=self.engine, query="SELECT 1 as value"),
            output=IcebergOutput(
                iceberg_catalog=self.catalog,
                iceberg_namespace=self.namespace,
                iceberg_table="test_table",
            ),
        )
        async def func(batch_input: daft.DataFrame, output, **kwargs):
            await output.write_batched_daft_dataframe(batch_input)

        await func(batch_input=None, output=None)

        table = self.catalog.load_table("default.test_table")
        data_scan = table.scan().to_arrow()
        assert len(data_scan) == 1

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_single_chunk(self, _):
        """
        Test to read the SQL data in a single chunk
        """

        self._create_test_clients(query=INSERT_QUERY)

        @transform_daft(
            batch_input=SQLQueryInput(
                engine=self.engine, query="SELECT * FROM numbers", chunk_size=None
            ),
            output=IcebergOutput(
                iceberg_catalog=self.catalog,
                iceberg_namespace=self.namespace,
                iceberg_table="test_table_two",
            ),
        )
        async def func(batch_input: daft.DataFrame, output, **kwargs):
            assert batch_input.count_rows() == 10
            await output.write_daft_dataframe(batch_input)

        await func(batch_input=None, output=None)

        table = self.catalog.load_table("default.test_table_two")
        data_scan = table.scan().to_arrow()
        assert len(data_scan) == 10

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_multiple_chunks(self, _):
        """
        Test to read the SQL data in multiple chunks
        """

        self._create_test_clients(query=INSERT_QUERY)
        expected_row_count = [3, 3, 3, 1]

        @transform_daft(
            batch_input=SQLQueryInput(
                engine=self.engine, query="SELECT * FROM numbers", chunk_size=3
            ),
            output=IcebergOutput(
                iceberg_catalog=self.catalog,
                iceberg_namespace=self.namespace,
                iceberg_table="test_table_three",
            ),
        )
        async def func(batch_input: AsyncIterator[daft.DataFrame], output, **kwargs):
            async for chunk in batch_input:
                assert chunk.count_rows() == expected_row_count.pop(0)
                await output.write_daft_dataframe(chunk)

        await func(batch_input=None, output=None)  # type: ignore

        table = self.catalog.load_table("default.test_table_three")
        data_scan = table.scan().to_arrow()
        assert len(data_scan) == 10

    async def test_iceberg_single_input_and_output(self):
        """
        Test to read the data from the iceberg table(INPUT), transform it
        and write it back to another iceberg table (OUTPUT)
        """
        table_two = self.catalog.load_table("default.test_table_two")

        @transform_daft(
            batch_input=IcebergInput(
                table=table_two,
                chunk_size=None,
            ),
            output=IcebergOutput(
                iceberg_catalog=self.catalog,
                iceberg_namespace=self.namespace,
                iceberg_table="test_table_four",
            ),
        )
        async def func(batch_input, output, **kwargs):
            await output.write_daft_dataframe(batch_input.transform(add_1))
            return batch_input

        await func(batch_input=None, output=None)

        table = self.catalog.load_table("default.test_table_four")
        data_scan = table.scan().to_arrow()
        assert len(data_scan) == 10
