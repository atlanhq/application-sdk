import os

import pandas as pd
import sqlalchemy
from sqlalchemy.sql import text

from application_sdk import activity_pd
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput


class TestDecorators:
    async def test_query_batch_basic(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @activity_pd(
            batch_input=lambda self: SQLQueryInput(engine, "SELECT 1 as value")
        )
        async def func(self, batch_input: pd.DataFrame, **kwargs):
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
        async def func(self, batch_input: pd.DataFrame, **kwargs):
            assert len(batch_input) == 3

        await func(self)

    async def test_query_write_basic(self):
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
        async def func(self, batch_input, out1, out2, **kwargs):
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

    async def test_json_input(self):
        # Create a sample JSON file for input
        input_file_path = "/tmp/raw/schema/1.json"
        os.makedirs(os.path.dirname(input_file_path), exist_ok=True)
        with open(input_file_path, "w") as f:
            f.write('{"value":1}\n{"value":2}\n')

        @activity_pd(
            batch_input=lambda self, arg: JsonInput(
                path="/tmp/raw/",
                batch=["schema/1.json"],
            ),
            out1=lambda self, arg: JsonOutput(
                output_path="/tmp/transformed/schema",
                upload_file_prefix="transformed",
            ),
        )
        async def func(self, batch_input, out1, **kwargs):
            await out1.write_df(batch_input.applymap(lambda x: x + 1))
            return batch_input

        arg = {}
        await func(self, arg)
        # Check files generated
        with open("/tmp/raw/schema/1.json") as f:
            assert f.read().strip() == '{"value":1}\n{"value":2}'
        with open("/tmp/transformed/schema/1.json") as f:
            assert f.read().strip() == '{"value":2}\n{"value":3}'
