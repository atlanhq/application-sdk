import os
from concurrent.futures import Future
from unittest.mock import patch

import pandas as pd
import sqlalchemy
from sqlalchemy.sql import text

from application_sdk import activity_pd
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput


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


class TestPandasDecorators:
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_basic(self, _):
        """
        Basic test to read the SQL data
        """
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(engine, "SELECT 1 as value")
        )
        async def func(self, batch_input: pd.DataFrame, **kwargs):
            assert len(batch_input) == 1

        await func(self)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_single_chunk(self, _):
        """
        Test to read the SQL data in a single chunk
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

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(
                engine, "SELECT * FROM numbers", chunk_size=None
            )
        )
        async def func(self, batch_input: pd.DataFrame, **kwargs):
            assert len(batch_input) == 10

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

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(
                engine, "SELECT * FROM numbers", chunk_size=3
            )
        )
        async def func(self, batch_input: pd.DataFrame, **kwargs):
            assert len(batch_input) == expected_row_count.pop(0)

        await func(self)

    async def test_json_input(self):
        # Create a sample JSON file for input
        input_file_path = "/tmp/tests/test_pandas_decorator/raw/schema/1.json"
        os.makedirs(os.path.dirname(input_file_path), exist_ok=True)
        with open(input_file_path, "w") as f:
            f.write('{"value":1}\n{"value":2}\n')

        @activity_pd(
            batch_input=lambda self, arg: JsonInput(
                path="/tmp/tests/test_pandas_decorator/raw/",
                file_names=["schema/1.json"],
            ),
            out1=lambda self, arg: JsonOutput(
                output_path="/tmp/tests/test_pandas_decorator/transformed/schema",
                upload_file_prefix="transformed",
            ),
        )
        async def func(self, batch_input, out1, **kwargs):
            await out1.write_df(batch_input.map(lambda x: x + 1))
            return batch_input

        arg = {}
        await func(self, arg)
        # Check files generated
        with open("/tmp/tests/test_pandas_decorator/raw/schema/1.json") as f:
            assert f.read().strip() == '{"value":1}\n{"value":2}'
        with open("/tmp/tests/test_pandas_decorator/transformed/schema/1.json") as f:
            assert f.read().strip() == '{"value":2}\n{"value":3}'
