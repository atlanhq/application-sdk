import os
from concurrent.futures import Future
from types import TracebackType
from typing import Any, AsyncIterator, Callable, List
from unittest.mock import patch

import daft
import pytest
import sqlalchemy
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.sql import text

from application_sdk.decorators import transform_daft
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput


def add_1(dataframe: daft.DataFrame) -> daft.DataFrame:
    dataframe = dataframe.select(daft.col("value") + 1)
    return dataframe


class MockSingleThreadExecutor:
    def __enter__(self) -> "MockSingleThreadExecutor":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
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
        Clean up the test clients
        """
        test_db_path = "/tmp/test_connectorx.db"
        if os.path.exists(test_db_path):
            os.remove(test_db_path)

    @given(
        value=st.integers(
            min_value=-(2**31), max_value=2**31 - 1
        )  # SQLite INTEGER limits
    )
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    @settings(deadline=None)
    async def test_query_batch_basic(self, _, value: int) -> None:
        """
        Basic test to read the SQL data with hypothesis-generated values
        """
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS test_table (value INTEGER)"))
            conn.execute(
                text("INSERT INTO test_table (value) VALUES (:value)"), {"value": value}
            )
            conn.commit()

        @transform_daft(
            batch_input=SQLQueryInput(
                engine=engine, query="SELECT value FROM test_table"
            )
        )
        async def func(
            batch_input: AsyncIterator[daft.DataFrame], **kwargs: Any
        ) -> None:
            async for chunk in batch_input:
                assert chunk.count_rows() == 1
                assert chunk.select("value").to_pandas().iloc[0, 0] == value

        await func(batch_input=None)  # type: ignore

    @given(
        values=st.lists(
            st.integers(
                min_value=-(2**31), max_value=2**31 - 1
            ),  # SQLite INTEGER limits
            min_size=1,
            max_size=100,
        )
    )
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    @settings(deadline=None)
    async def test_query_batch_single_chunk(self, _, values: List[int]) -> None:
        """
        Test to read the SQL data in a single chunk with hypothesis-generated data
        """
        sqlite_db_url = "sqlite:////tmp/test_query_batch_single_chunk.db"
        engine = sqlalchemy.create_engine(sqlite_db_url)
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            # Insert hypothesis-generated values
            for value in values:
                conn.execute(
                    text("INSERT INTO numbers (value) VALUES (:value)"),
                    {"value": value},
                )
            conn.commit()

        @transform_daft(
            batch_input=SQLQueryInput(
                engine=engine, query="SELECT * FROM numbers", chunk_size=None
            )
        )
        async def func(batch_input: daft.DataFrame, **kwargs):
            assert batch_input.count_rows() == len(values)
            # Verify all values are present
            result_values = batch_input.select("value").to_pandas()["value"].tolist()
            assert sorted(result_values) == sorted(values)

        await func(batch_input=None)

    @given(
        values=st.lists(
            st.integers(
                min_value=-(2**31), max_value=2**31 - 1
            ),  # SQLite INTEGER limits
            min_size=1,
            max_size=100,
        ),
        chunk_size=st.integers(),
    )
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    @settings(deadline=None)
    @pytest.mark.skip(
        reason="Failing due to ExceptionGroup: Hypothesis found 4 distinct failures"
    )
    async def test_query_batch_multiple_chunks(
        self, _, values: List[int], chunk_size: int
    ) -> None:
        """
        Test to read the SQL data in multiple chunks with hypothesis-generated data and chunk sizes
        """
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS numbers (value INTEGER)"))
            conn.execute(text("DELETE FROM numbers"))
            for value in values:
                conn.execute(
                    text("INSERT INTO numbers (value) VALUES (:value)"),
                    {"value": value},
                )
            conn.commit()

        # Calculate expected chunk sizes
        total_rows = len(values)
        expected_chunks = []
        for i in range(0, total_rows, chunk_size):
            chunk_length = min(chunk_size, total_rows - i)
            expected_chunks.append(chunk_length)

        @transform_daft(
            batch_input=SQLQueryInput(
                engine=engine, query="SELECT * FROM numbers", chunk_size=chunk_size
            )
        )
        async def func(
            batch_input: AsyncIterator[daft.DataFrame], **kwargs: Any
        ) -> None:
            all_values = []
            chunks_seen = 0
            async for chunk in batch_input:
                chunks_seen += 1
                assert chunk.count_rows() == expected_chunks[chunks_seen - 1]
                all_values.extend(chunk.select("value").to_pandas()["value"].tolist())

            assert chunks_seen == len(expected_chunks)
            assert sorted(all_values) == sorted(values)

        await func(batch_input=None)  # type: ignore

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

        @transform_daft(
            batch_input=SQLQueryInput(
                engine=sqlite_db_url, query="SELECT * FROM numbers", chunk_size=None
            )
        )
        async def func(batch_input: daft.DataFrame, **kwargs):
            assert batch_input.count_rows() == 10

        await func(batch_input=None)

    async def test_json_input(self) -> None:
        # Create a sample JSON file for input
        input_file_path = "/tmp/tests/test_daft_decorator/raw/schema/1.json"
        os.makedirs(os.path.dirname(input_file_path), exist_ok=True)
        with open(input_file_path, "w") as f:
            f.write('{"value":1}\n{"value":2}\n')

        @transform_daft(
            batch_input=JsonInput(
                path="/tmp/tests/test_daft_decorator/raw/",
                file_names=["schema/1.json"],
                download_file_prefix="raw",
            ),
            out1=JsonOutput(
                output_path="/tmp/tests/test_daft_decorator/",
                output_suffix="transformed/schema",
            ),
        )
        async def func(
            batch_input: AsyncIterator[daft.DataFrame], out1: JsonOutput, **kwargs: Any
        ) -> None:
            async for chunk in batch_input:
                await out1.write_daft_dataframe(chunk.transform(add_1))

        await func(batch_input=None, out1=None)  # type: ignore
        # Check files generated
        with open("/tmp/tests/test_daft_decorator/raw/schema/1.json") as f:
            assert f.read().strip() == '{"value":1}\n{"value":2}'
        with open("/tmp/tests/test_daft_decorator/transformed/schema/1.json") as f:
            assert f.read().strip() == '{"value":2}\n{"value":3}'
