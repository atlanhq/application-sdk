from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Type, TypeVar
from unittest.mock import patch

import pandas as pd
import pytest
import sqlalchemy
from hypothesis import HealthCheck, given, settings

from application_sdk.decorators import transform
from application_sdk.inputs.json import JsonInput
from application_sdk.inputs.sql_query import SQLQueryInput
from application_sdk.outputs.json import JsonOutput
from tests.hypothesis.strategies.pandas_decorators import (
    dataframe_strategy,
    json_input_config_strategy,
    json_output_config_strategy,
    sql_input_config_strategy,
)

T = TypeVar("T")


class MockSingleThreadExecutor:
    def __enter__(self) -> "MockSingleThreadExecutor":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[Any],
    ) -> None:
        pass

    def submit(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future[T]:
        future: Future[T] = Future()
        try:
            # Execute the function synchronously
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        return future


# Configure Hypothesis settings at the module level
settings.register_profile(
    "pandas_decorators",
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile("pandas_decorators")


class TestPandasDecorators:
    @pytest.mark.asyncio
    @settings(max_examples=5)
    @given(sql_config=sql_input_config_strategy)
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_basic(
        self, mock_executor: Any, sql_config: Dict[str, Any]
    ) -> None:
        """Test basic SQL query execution"""
        engine = sqlalchemy.create_engine("sqlite:///:memory:")

        @transform(
            batch_input=SQLQueryInput(
                engine=engine,
                query=sql_config["query"],
                chunk_size=sql_config["chunk_size"],
            )
        )
        async def func(batch_input: pd.DataFrame, **kwargs: Any) -> None:
            assert isinstance(batch_input, pd.DataFrame)

        await func()

    @pytest.mark.asyncio
    @settings(max_examples=5)
    @given(df=dataframe_strategy(min_rows=1, max_rows=10))
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_single_chunk(
        self, mock_executor: Any, df: pd.DataFrame
    ) -> None:
        """Test SQL query execution with a single chunk"""
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        table_name = "test_table"

        # Create table and insert data
        df.to_sql(table_name, engine, if_exists="replace", index=False)

        @transform(
            batch_input=SQLQueryInput(
                engine=engine, query=f"SELECT * FROM {table_name}", chunk_size=None
            )
        )
        async def func(batch_input: pd.DataFrame, **kwargs: Any) -> None:
            # Reset index to ensure comparison works correctly
            batch_input = batch_input.reset_index(drop=True)
            df_expected = df.reset_index(drop=True)

            # Ensure same column order
            batch_input = batch_input[df_expected.columns]

            # Compare DataFrames
            pd.testing.assert_frame_equal(
                batch_input,
                df_expected,
                check_dtype=False,  # Allow for type coercion in SQLite
            )

        await func()

    @pytest.mark.asyncio
    @settings(max_examples=5)
    @given(df=dataframe_strategy(min_rows=5, max_rows=10))
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_query_batch_multiple_chunks(
        self, mock_executor: Any, df: pd.DataFrame
    ) -> None:
        """Test SQL query execution with multiple chunks"""
        engine = sqlalchemy.create_engine("sqlite:///:memory:")
        table_name = "test_table"
        chunk_size = 3

        # Create table and insert data
        df.to_sql(table_name, engine, if_exists="replace", index=False)

        total_rows = 0

        @transform(
            batch_input=SQLQueryInput(
                engine=engine,
                query=f"SELECT * FROM {table_name}",
                chunk_size=chunk_size,
            )
        )
        async def func(batch_input: pd.DataFrame, **kwargs: Any) -> None:
            nonlocal total_rows
            for chunk in batch_input:
                # Verify chunk size
                if total_rows + chunk_size <= len(df):
                    assert len(chunk) == chunk_size
                else:
                    assert len(chunk) == len(df) % chunk_size
                total_rows += len(chunk)

        await func()

        # Verify total number of rows processed
        assert total_rows == len(df)

    @pytest.mark.asyncio
    @settings(max_examples=5)
    @given(
        df=dataframe_strategy(min_rows=1, max_rows=5),
        json_in_config=json_input_config_strategy,
        json_out_config=json_output_config_strategy,
    )
    async def test_json_input(
        self,
        df: pd.DataFrame,
        json_in_config: Dict[str, Any],
        json_out_config: Dict[str, Any],
    ) -> None:
        """Test JSON input and output with DataFrame transformation"""
        # Create input directory and file
        input_path = Path(json_in_config["path"])
        input_file = input_path / json_in_config["file_names"][0]
        input_path.mkdir(parents=True, exist_ok=True)

        # Create output directory
        output_path = (
            Path(json_out_config["output_path"]) / json_out_config["output_suffix"]
        )
        output_path.mkdir(parents=True, exist_ok=True)

        # Write test data
        df.to_json(str(input_file), orient="records", lines=True)

        @transform(
            batch_input=JsonInput(
                path=str(input_path),
                file_names=json_in_config["file_names"],
                download_file_prefix=json_in_config["download_file_prefix"],
            ),
            out1=JsonOutput(
                output_path=str(output_path.parent),
                output_suffix=output_path.name,
            ),
        )
        async def func(batch_input: Any, out1: JsonOutput, **kwargs: Any) -> None:
            async for chunk in batch_input:
                # Transform: increment all numeric values by 1
                numeric_cols = chunk.select_dtypes(include=["int64", "float64"]).columns
                transformed = chunk.copy()
                for col in numeric_cols:
                    transformed[col] = chunk[col] + 1
                await out1.write_dataframe(transformed)

        await func()

        # Verify output
        output_file = output_path / "1.json"
        assert output_file.exists()

        # Read and verify transformed data
        output_df = pd.read_json(str(output_file), lines=True)
        input_df = pd.read_json(str(input_file), lines=True)

        # Check that numeric columns were incremented
        numeric_cols = input_df.select_dtypes(include=["int64", "float64"]).columns
        for col in numeric_cols:
            pd.testing.assert_series_equal(
                output_df[col], input_df[col] + 1, check_names=False
            )
