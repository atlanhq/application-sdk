from concurrent.futures import Future
from unittest.mock import patch

import pandas as pd
import sqlalchemy
from sqlalchemy.sql import text

from application_sdk import activity_pd
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


class TestDecorators:
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_basic(self, _):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(engine, "SELECT 1 as value")
        )
        async def func(self, batch_input: pd.DataFrame):
            assert len(batch_input) == 1

        await func(self)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_multiple_chunks(self, _):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(text("INSERT INTO numbers (value) VALUES (0), (1), (2)"))
            conn.commit()

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(engine, "SELECT * FROM numbers")
        )
        async def func(self, batch_input: pd.DataFrame):
            assert len(batch_input) == 3

        await func(self)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_write_basic(self, _):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_pd(
            batch_input=lambda self, arg: SQLQueryInput(engine, "SELECT 1 as value"),
            out1=lambda self, arg: JsonOutput(
                output_path="/tmp/raw/table", upload_file_prefix="raw"
            ),
            out2=lambda self, arg: JsonOutput(
                output_path="/tmp/transformed/table",
                upload_file_prefix="transformed",
            ),
        )
        async def func(self, batch_input, out1, out2):
            await out1.write_df(batch_input)
            await out2.write_df(batch_input.map(lambda x: x + 1))
            return batch_input

        arg = {
            "metadata_sql": "SELECT * FROM information_schema.tables",
        }
        await func(self, arg)
        # Check files generated
        with open("/tmp/raw/table/1.json") as f:
            assert f.read().strip() == '{"value":1}'
        with open("/tmp/transformed/table/1.json") as f:
            assert f.read().strip() == '{"value":2}'
