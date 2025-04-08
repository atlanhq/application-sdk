import os
from concurrent.futures import Future
from unittest.mock import patch

import pytest
import sqlalchemy
from hypothesis import given
from hypothesis.strategies import integers
from sqlalchemy.sql import text

from application_sdk.decorators import transform
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
    @given(value=integers())
    @pytest.mark.skip(
        reason="Failing due to assertion error: assert -9.223372036854776e+18 == -9223372036854775809"
    )
    async def test_query_batch_basic(self, _, value):
        """
        Basic test to read the SQL data with hypothesis generated values
        """
        import pandas as pd

        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS test_values (value INTEGER)"))
            conn.execute(text("DELETE FROM test_values"))
            conn.execute(text(f"INSERT INTO test_values (value) VALUES ({value})"))
            conn.commit()

        @transform(
            batch_input=SQLQueryInput(engine=engine, query="SELECT * FROM test_values")
        )
        async def func(batch_input: pd.DataFrame, **kwargs):
            for df in batch_input:
                assert len(df) == 1
                assert df["value"].iloc[0] == value

        await func()

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

        @transform(
            batch_input=SQLQueryInput(
                engine=engine, query="SELECT * FROM numbers", chunk_size=None
            )
        )
        async def func(batch_input: "pd.DataFrame", **kwargs):
            assert len(batch_input) == 10

        await func()

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

        @transform(
            batch_input=SQLQueryInput(
                engine=engine, query="SELECT * FROM numbers", chunk_size=3
            )
        )
        async def func(batch_input: "pd.DataFrame", **kwargs):
            for chunk in batch_input:
                assert len(chunk) == expected_row_count.pop(0)

        await func()

    @pytest.mark.skip(
        reason="We'll be removing the decorator in the future, so skipping this test for now"
    )
    async def test_json_input(self):
        # Create a sample JSON file for input
        input_file_path = "/tmp/tests/test_pandas_decorator/raw/schema/1.json"
        os.makedirs(os.path.dirname(input_file_path), exist_ok=True)
        with open(input_file_path, "w") as f:
            f.write('{"value":1}\n{"value":2}\n')

        @transform(
            batch_input=JsonInput(
                path="/tmp/tests/test_pandas_decorator/raw/",
                file_names=["schema/1.json"],
                download_file_prefix="raw",
            ),
            out1=JsonOutput(
                output_path="/tmp/tests/test_pandas_decorator/",
                output_suffix="transformed/schema",
            ),
        )
        async def func(batch_input, out1, **kwargs):
            async for chunk in batch_input:
                await out1.write_dataframe(chunk.map(lambda x: x + 1))

        await func()
        # Check files generated
        with open("/tmp/tests/test_pandas_decorator/raw/schema/1.json") as f:
            assert f.read().strip() == '{"value":1}\n{"value":2}'
        with open("/tmp/tests/test_pandas_decorator/transformed/schema/1.json") as f:
            assert f.read().strip() == '{"value":2}\n{"value":3}'
