import os
from concurrent.futures import Future
from unittest.mock import patch

import daft
import pandas as pd

from application_sdk.decorators import transform_daft
from application_sdk.inputs.parquet import ParquetInput
from application_sdk.outputs.parquet import ParquetOutput

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


def add_1(dataframe):
    """
    Similar to a transformation function that adds 1 to the value column
    """
    dataframe = dataframe.select(daft.col("value") + 1)
    return dataframe


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


class TestDaftDecoratorsParquet:
    @classmethod
    def setup_class(cls):
        """
        Method to setup the test data
        """
        cls.test_dir = "/tmp/tests/parquet"
        os.makedirs(cls.test_dir, exist_ok=True)
        
        # Create test parquet file
        df = pd.DataFrame(TEST_DATA)
        cls.input_file = f"{cls.test_dir}/input.parquet"
        df.to_parquet(cls.input_file)

    @classmethod
    def teardown_class(cls):
        """
        Clean up the test files
        """
        if os.path.exists(cls.input_file):
            os.remove(cls.input_file)
        
        # Clean up output files
        for file in os.listdir(cls.test_dir):
            if file.startswith("output"):
                os.remove(os.path.join(cls.test_dir, file))
        
        os.rmdir(cls.test_dir)

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_parquet_single_input_and_output(self, _):
        """
        Test to read the data from a parquet file (INPUT), transform it
        and write it back to another parquet file (OUTPUT)
        """
        @transform_daft(
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
        async def func(batch_input, output, **kwargs):
            await output.write_daft_dataframe(batch_input.transform(add_1))
            return batch_input

        await func()

        # Verify output
        output_file = f"{self.test_dir}/output_1.parquet"
        assert os.path.exists(output_file)
        
        # Read and verify transformed data
        df = pd.read_parquet(output_file)
        assert len(df) == 10
        assert all(df["value"] == pd.Series(range(1, 11)))  # Original values + 1

    @patch(
        "concurrent.futures.ThreadPoolExecutor",
        side_effect=MockSingleThreadExecutor,
    )
    async def test_parquet_batch_processing(self, _):
        """
        Test to read the parquet data in batches
        """
        @transform_daft(
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
        async def func(batch_input, output, **kwargs):
            async for chunk in batch_input:
                await output.write_daft_dataframe(chunk.transform(add_1))

        await func()

        # Verify output files exist
        output_files = [
            f for f in os.listdir(f"{self.test_dir}")
            if f.startswith("output_batch_")
        ]
        assert len(output_files) == 4  # 10 records split into chunks of 3

        # Read and verify all transformed data
        all_data = []
        for file in sorted(output_files):
            df = pd.read_parquet(f"{self.test_dir}/{file}")
            all_data.extend(df["value"].tolist())

        assert len(all_data) == 10
        assert all_data == list(range(1, 11))  # Original values + 1 