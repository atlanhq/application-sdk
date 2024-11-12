import sqlalchemy
from sqlalchemy.sql import text
from application_sdk.workflows.sql.decorators import stream_query_results


class TestDecorators:
    async def test_stream_query_results_basic_func(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @stream_query_results(engine, "SELECT 1")
        async def func(chunk_number, chunk_df):
            assert chunk_number == 0
            assert len(chunk_df) == 1

        await func()

    async def test_stream_query_results_multiple_chunks(self):
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            conn.execute(text("INSERT INTO numbers (value) VALUES (0), (1), (2)"))
            conn.commit()

        @stream_query_results(engine, "SELECT * FROM numbers", chunk_size=2)
        async def func(chunk_number, chunk_df):
            if chunk_number == 0:
                assert len(chunk_df) == 2
            elif chunk_number == 1:
                assert len(chunk_df) == 1

        await func()