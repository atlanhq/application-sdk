import duckdb
import pandas as pd
import sqlalchemy
from sqlalchemy.sql import text
from application_sdk.workflows.sql.decorators import transform, QueryInput, JsonOutput


class TestDecorators:
    async def test_query_batch_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @transform(
            batch_input=QueryInput(engine, "SELECT 1 as value")
        )
        async def func(batch_input: pd.DataFrame):
            assert len(batch_input) == 1

        await func()

    async def test_query_batch_multiple_chunks(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(text("INSERT INTO numbers (value) VALUES (0), (1), (2)"))
            conn.commit()

        @transform(
            batch_input=QueryInput(engine, "SELECT * FROM numbers")
        )
        async def func(batch_input: pd.DataFrame):
            assert len(batch_input) == 3

        await func()

    async def test_query_write_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @transform(
            batch_input=QueryInput(engine, "SELECT 1 as value"),
            out1=JsonOutput("/tmp/raw"),
            out2=JsonOutput("/tmp/transformed")
        )
        async def func(batch_input, out1, out2):
            return [batch_input, batch_input.map(lambda x: x + 1)]


        await func()
        # Check files generated
        with open("/tmp/raw/0.json") as f:
            assert f.read().strip() == '{"value":1}'
        with open("/tmp/transformed/0.json") as f:
            assert f.read().strip() == '{"value":2}'

