import pandas as pd
import sqlalchemy
from sqlalchemy.sql import text
from application_sdk.workflows.sql.decorators import query_batch, transform_query_results
import pytest


class TestDecorators:
    async def test_query_batch_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @query_batch(engine, "SELECT 1")
        async def func(chunk_number, chunk_df):
            assert chunk_number == 0
            assert len(chunk_df) == 1

        await func()

    async def test_query_batch_multiple_chunks(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(text("INSERT INTO numbers (value) VALUES (0), (1), (2)"))
            conn.commit()

        @query_batch(engine, "SELECT * FROM numbers", chunk_size=2)
        async def func(chunk_number, chunk_df):
            if chunk_number == 0:
                assert len(chunk_df) == 2
            elif chunk_number == 1:
                assert len(chunk_df) == 1

        await func()

    async def test_query_write_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @transform_query_results(engine, "SELECT 1 as value", "/tmp")
        async def func(chunk_number, chunk_df: pd.DataFrame):
            assert chunk_number == 0
            assert len(chunk_df) == 1
            return chunk_df.applymap(lambda x: x + 1)

        await func()
        # Check files generated
        with open("/tmp/raw/0.json") as f:
            assert f.read().strip() == '{"value":1}'
        with open("/tmp/transformed/0.json") as f:
            assert f.read().strip() == '{"value":2}'

    async def test_query_write_no_return_func(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @transform_query_results(engine, "SELECT 1", "/tmp")
        async def func(chunk_number, chunk_df):
            pass

        with pytest.raises(ValueError):
            await func()
