import pandas as pd
import sqlalchemy
from sqlalchemy.sql import text

from application_sdk import activity_pd
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput


class TestDecorators:
    async def test_query_batch_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(engine, "SELECT 1 as value")
        )
        async def func(self, batch_input: pd.DataFrame):
            assert len(batch_input) == 1

        await func(self)

    async def test_query_batch_multiple_chunks(self):
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

    async def test_query_write_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_pd(
            batch_input=lambda self, arg: SQLQueryInput(engine, "SELECT 1 as value"),
            out1=lambda self, arg: JsonOutput(
                output_path="/tmp/raw", upload_file_prefix="raw", typename="table"
            ),
            out2=lambda self, arg: JsonOutput(
                output_path="/tmp/transformed",
                upload_file_prefix="transformed",
                typename="table",
            ),
        )
        async def func(self, batch_input, out1, out2):
            return {"out1": batch_input, "out2": batch_input.map(lambda x: x + 1)}

        arg = {
            "metadata_sql": "SELECT * FROM information_schema.tables",
        }
        await func(self, arg)
        # Check files generated
        with open("/tmp/raw/table-1.json") as f:
            assert f.read().strip() == '{"value":1}'
        with open("/tmp/transformed/table-1.json") as f:
            assert f.read().strip() == '{"value":2}'
