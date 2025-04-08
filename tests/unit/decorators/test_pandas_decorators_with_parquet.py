import os
import tempfile
import types
from concurrent.futures import Future
from typing import Any, AsyncIterator, Callable, List, Optional, TypeVar
from unittest.mock import patch

import pytest

from application_sdk.decorators import transform
from application_sdk.inputs.parquet import ParquetInput
from application_sdk.outputs.parquet import ParquetOutput

T = TypeVar("T")

TEST_DATA = [
    {"value": 0},
    {"value": 1},
    {"value": 2},
    {"value": 3},
    {"value": 4},
    {"value": 5},
    {"value": 6},
    {"value": 7},
    {"value": 8},
    {"value": 9},
]


def add_1(dataframe: "pd.DataFrame") -> "pd.DataFrame":
    """
    Similar to a transformation function that adds 1 to the value column
    """
    dataframe["value"] = dataframe["value"] + 1
    return dataframe


class MockSingleThreadExecutor:
    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[types.TracebackType],
    ) -> None:
        pass

    def submit(
        self,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> Future[T]:
        future: Future[T] = Future()
        try:
            # Execute the function synchronously
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
        return future


class TestPandasDecoratorsParquet:
    @classmethod
    def setup_class(cls):
        """
        Method to setup the test data
        """
        cls.test_dir = tempfile.mkdtemp(prefix="parquet_pandas_test_")

        # Create test parquet file
        import pandas as pd

        df = pd.DataFrame(TEST_DATA)
        cls.input_file = os.path.join(cls.test_dir, "input.parquet")
        df.to_parquet(cls.input_file)

    @classmethod
    def teardown_class(cls):
        """
        Clean up the test files
        """
        try:
            if os.path.exists(cls.input_file):
                os.remove(cls.input_file)

            # Clean up output files
            for file in os.listdir(cls.test_dir):
                if file.startswith("output"):
                    os.remove(os.path.join(cls.test_dir, file))

            os.rmdir(cls.test_dir)
        except Exception as e:
            print(f"Warning: Failed to clean up test files: {e}")

    @pytest.mark.skip(
        reason="We'll be removing the decorator in the future, so skipping this test for now"
    )
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    @patch("application_sdk.inputs.parquet.ParquetInput.read_file")
    @patch("application_sdk.outputs.parquet.ParquetOutput.upload_file")
    async def test_parquet_single_input_and_output(self, mock_upload, mock_read, _):
        """
        Test to read the data from a parquet file (INPUT), transform it
        and write it back to another parquet file (OUTPUT)
        """
        # Configure mock for read_file to return the input file path
        mock_read.return_value = self.input_file
        # Configure mock for upload_file to do nothing
        mock_upload.return_value = None

        @transform(
            batch_input=ParquetInput(
                file_path=self.input_file,
                chunk_size=None,
            ),
            output=ParquetOutput(
                output_path=self.test_dir,
                output_suffix="/output",
                output_prefix="test",
                mode="overwrite",
            ),
        )
        async def func(
            batch_input: "pd.DataFrame", output: ParquetOutput, **kwargs: Any
        ) -> "pd.DataFrame":
            await output.write_dataframe(add_1(batch_input))
            return batch_input

        await func()

        # Verify output
        output_file = f"{self.test_dir}/output_1.parquet"
        assert os.path.exists(output_file)

        # Read and verify transformed data
        df = pd.read_parquet(output_file)
        assert len(df) == 10
        assert all(df["value"] == pd.Series(range(1, 11)))  # Original values + 1

    @pytest.mark.skip(
        reason="We'll be removing the decorator in the future, so skipping this test for now"
    )
    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    @patch("application_sdk.inputs.parquet.ParquetInput.read_file")
    @patch("application_sdk.outputs.parquet.ParquetOutput.upload_file")
    async def test_parquet_batch_processing(self, mock_upload, mock_read, _):
        """
        Test to read the parquet data in batches
        """
        # Configure mock for read_file to return the input file path
        mock_read.return_value = self.input_file
        # Configure mock for upload_file to do nothing
        mock_upload.return_value = None

        batch_count = 0

        @transform(
            batch_input=ParquetInput(
                file_path=self.input_file,
                chunk_size=3,
            ),
            output=ParquetOutput(
                output_path=self.test_dir,
                output_suffix="/output_batch",
                output_prefix="test_batch",
                mode="overwrite",
            ),
        )
        async def func(
            batch_input: AsyncIterator["pd.DataFrame"],
            output: ParquetOutput,
            **kwargs: Any,
        ) -> None:
            nonlocal batch_count
            async for chunk in batch_input:
                await output.write_dataframe(add_1(chunk))
                batch_count += 1

        await func()

        # Verify output files exist
        output_files = [
            f for f in os.listdir(f"{self.test_dir}") if f.startswith("output_batch_")
        ]
        assert len(output_files) == 4  # 10 records split into chunks of 3
        assert batch_count == 4  # Verify we processed 4 batches

        # Read and verify all transformed data
        all_data: List[int] = []
        for file in sorted(output_files):
            import pandas as pd

            df = pd.read_parquet(f"{self.test_dir}/{file}")
            all_data.extend(df["value"].tolist())

        assert len(all_data) == 10
        assert all_data == list(range(1, 11))  # Original values + 1
