import os
from concurrent.futures import Future
from unittest.mock import patch

import daft
import sqlalchemy
from sqlalchemy.sql import text

from application_sdk import activity_daft
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput


def add_1(df):
    df = df.select(daft.col("value") + 1)
    return df


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


class TestDaftDecorators:
    @classmethod
    def teardown_class(cls):
        """
        Clean up the test resources
        """
        os.remove("/tmp/test_connectorx.db")

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_basic(self, _):
        """
        Basic test to read the SQL data
        """
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_daft(
            batch_input=lambda self: SQLQueryInput(engine, "SELECT 1 as value")
        )
        async def func(self, batch_input: daft.DataFrame, **kwargs):
            assert batch_input.count_rows() == 1

        await func(self)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_single_chunk(self, _):
        """
        Test to read the SQL data in a single chunk
        """
        sqlite_db_url = "sqlite:////tmp/test_query_batch_single_chunk.db"
        engine = sqlalchemy.create_engine(sqlite_db_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(
                text(
                    "INSERT INTO numbers (value) VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)"
                )
            )
            conn.commit()

        @activity_daft(
            batch_input=lambda self: SQLQueryInput(
                engine, "SELECT * FROM numbers", chunk_size=None
            )
        )
        async def func(self, batch_input: daft.DataFrame, **kwargs):
            assert batch_input.count_rows() == 10

        await func(self)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_multiple_chunks(self, _):
        """
        Test to read the SQL data in multiple chunks
        """
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(
                text(
                    "INSERT INTO numbers (value) VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)"
                )
            )
            conn.commit()

        expected_row_count = [3, 3, 3, 1]

        @activity_daft(
            batch_input=lambda self: SQLQueryInput(
                engine, "SELECT * FROM numbers", chunk_size=3
            )
        )
        async def func(self, batch_input: daft.DataFrame, **kwargs):
            assert batch_input.count_rows() == expected_row_count.pop(0)

        await func(self)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_multiple_chunks_url(self, _):
        """
        Test to read the SQL data in multiple chunks using URL to enable ConnectorX processing
        """
        sqlite_db_url = "sqlite:////tmp/test_connectorx.db"
        engine = sqlalchemy.create_engine(sqlite_db_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(
                text(
                    "INSERT INTO numbers (value) VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)"
                )
            )
            conn.commit()

        @activity_daft(
            batch_input=lambda self: SQLQueryInput(
                sqlite_db_url, "SELECT * FROM numbers", chunk_size=None
            )
        )
        async def func(self, batch_input: daft.DataFrame, **kwargs):
            assert batch_input.count_rows() == 10

        await func(self)

    async def test_json_input(self):
        # Create a sample JSON file for input
        input_file_path = "/tmp/tests/test_daft_decorator/raw/schema/1.json"
        os.makedirs(os.path.dirname(input_file_path), exist_ok=True)
        with open(input_file_path, "w") as f:
            f.write('{"value":1}\n{"value":2}\n')

        @activity_daft(
            batch_input=lambda self, arg: JsonInput(
                path="/tmp/tests/test_daft_decorator/raw/",
                file_suffixes=["schema/1.json"],
            ),
            out1=lambda self, arg: JsonOutput(
                output_path="/tmp/tests/test_daft_decorator/transformed/schema",
                upload_file_prefix="transformed",
            ),
        )
        async def func(self, batch_input, out1, **kwargs):
            await out1.write_daft_df(batch_input.transform(add_1))
            return batch_input

        arg = {}
        await func(self, arg)
        # Check files generated
        with open("/tmp/tests/test_daft_decorator/raw/schema/1.json") as f:
            assert f.read().strip() == '{"value":1}\n{"value":2}'
        with open("/tmp/tests/test_daft_decorator/transformed/schema/1.json") as f:
            assert f.read().strip() == '{"value":2}\n{"value":3}'
