from typing import TYPE_CHECKING, AsyncIterator, Iterator, List, Optional, Union

from application_sdk.inputs import Input
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import daft  # type: ignore
    import pandas as pd


class ParquetInput(Input):
    """
    Parquet Input class to read data from Parquet files using daft and pandas.
    Supports reading both single files and directories containing multiple parquet files.
    """

    _EXTENSION = ".parquet"

    def __init__(
        self,
        path: str,
        chunk_size: int = 100000,
        buffer_size: int = 5000,
        file_names: Optional[List[str]] = None,
    ):
        """Initialize the Parquet input class.

        Args:
            path (str): Path to parquet file or directory containing parquet files.
                It accepts both types of paths:
                local path or object store path
                Wildcards are not supported.
            chunk_size (int): Number of rows per batch. Defaults to 100000.
            buffer_size (int): Number of rows per batch. Defaults to 5000.
            file_names (Optional[List[str]]): List of file names to read. Defaults to None.

        Raises:
            ValueError: When path is not provided or when single file path is combined with file_names
        """

        # Validate that single file path and file_names are not both specified
        if path.endswith(self._EXTENSION) and file_names:
            raise ValueError(
                f"Cannot specify both a single file path ('{path}') and file_names filter. "
                f"Either provide a directory path with file_names, or specify the exact file path without file_names."
            )

        self.path = path
        self.chunk_size = chunk_size
        self.buffer_size = buffer_size
        self.file_names = file_names

    async def get_dataframe(self) -> "pd.DataFrame":
        """Read data from parquet file(s) and return as pandas DataFrame.

        Returns:
            pd.DataFrame: Combined dataframe from specified parquet files

        Raises:
            ValueError: When no valid path can be determined or no matching files found
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file3.parquet"]:
        +-------+-------+-------+
        | col1  | col2  | col3  |
        +-------+-------+-------+
        | val1  | val2  | val3  |  # from file1.parquet
        | val7  | val8  | val9  |  # from file3.parquet
        +-------+-------+-------+

        Transformations:
        - Only specified files are read and combined
        - Column schemas must be compatible across files
        - Only reads files in the specified directory
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            parquet_files = await self.download_files()
            logger.info(f"Reading {len(parquet_files)} parquet files")

            return pd.concat(
                (pd.read_parquet(parquet_file) for parquet_file in parquet_files),
                ignore_index=True,
            )
        except Exception as e:
            logger.error(f"Error reading data from parquet file(s): {str(e)}")
            raise

    async def get_batched_dataframe(
        self,
    ) -> Union[AsyncIterator["pd.DataFrame"], Iterator["pd.DataFrame"]]:
        """Read data from parquet file(s) in batches as pandas DataFrames.

        Returns:
            AsyncIterator[pd.DataFrame]: Async iterator of pandas dataframes

        Raises:
            ValueError: When no parquet files found locally or in object store
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file2.parquet"] and chunk_size=2:
        Batch 1:
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val1  | val2  |  # from file1.parquet
        | val3  | val4  |  # from file1.parquet
        +-------+-------+

        Batch 2:
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val5  | val6  |  # from file2.parquet
        | val7  | val8  |  # from file2.parquet
        +-------+-------+

        Transformations:
        - Only specified files are combined then split into chunks
        - Each batch is a separate DataFrame
        - Only reads files in the specified directory
        """
        try:
            import pandas as pd

            # Ensure files are available (local or downloaded)
            parquet_files = await self.download_files()
            logger.info(f"Reading {len(parquet_files)} parquet files in batches")

            # Process each file individually to maintain memory efficiency
            for parquet_file in parquet_files:
                df = pd.read_parquet(parquet_file)
                for i in range(0, len(df), self.chunk_size):
                    yield df.iloc[i : i + self.chunk_size]
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) in batches: {str(e)}"
            )
            raise

    async def get_daft_dataframe(self) -> "daft.DataFrame":  # noqa: F821
        """Read data from parquet file(s) and return as daft DataFrame.

        Returns:
            daft.DataFrame: Combined daft dataframe from specified parquet files

        Raises:
            ValueError: When no parquet files found locally or in object store
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file3.parquet"]:
        +-------+-------+-------+
        | col1  | col2  | col3  |
        +-------+-------+-------+
        | val1  | val2  | val3  |  # from file1.parquet
        | val7  | val8  | val9  |  # from file3.parquet
        +-------+-------+-------+

        Transformations:
        - Only specified parquet files combined into single daft DataFrame
        - Lazy evaluation for better performance
        - Column schemas must be compatible across files
        """
        try:
            import daft  # type: ignore

            # Ensure files are available (local or downloaded)
            parquet_files = await self.download_files()
            logger.info(f"Reading {len(parquet_files)} parquet files with daft")

            # Use the discovered/downloaded files directly
            return daft.read_parquet(parquet_files)
        except Exception as e:
            logger.error(
                f"Error reading data from parquet file(s) using daft: {str(e)}"
            )
            raise

    async def get_batched_daft_dataframe(self) -> AsyncIterator["daft.DataFrame"]:  # type: ignore
        """Get batched daft dataframe from parquet file(s).

        Returns:
            AsyncIterator[daft.DataFrame]: An async iterator of daft DataFrames, each containing
            a batch of data from individual parquet files

        Raises:
            ValueError: When no parquet files found locally or in object store
            Exception: When reading parquet files fails

        Example transformation:
        Input files:
        +------------------+
        | file1.parquet    |
        | file2.parquet    |
        | file3.parquet    |
        +------------------+

        With file_names=["file1.parquet", "file3.parquet"]:
        Batch 1 (file1.parquet):
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val1  | val2  |
        | val3  | val4  |
        +-------+-------+

        Batch 2 (file3.parquet):
        +-------+-------+
        | col1  | col2  |
        +-------+-------+
        | val7  | val8  |
        | val9  | val10 |
        +-------+-------+

        Transformations:
        - Each specified file becomes a separate daft DataFrame batch
        - Lazy evaluation for better performance
        - Files processed individually for memory efficiency
        """
        try:
            import daft  # type: ignore

            # Ensure files are available (local or downloaded)
            parquet_files = await self.download_files()
            logger.info(f"Reading {len(parquet_files)} parquet files as daft batches")

            # Create a lazy dataframe without loading data into memory
            lazy_df = daft.read_parquet(parquet_files)

            # Get total count efficiently
            total_rows = lazy_df.count_rows()

            # Yield chunks without loading everything into memory
            for offset in range(0, total_rows, self.buffer_size):
                chunk = lazy_df.offset(offset).limit(self.buffer_size)
                yield chunk

            del lazy_df

        except Exception as error:
            logger.error(
                f"Error reading data from parquet file(s) in batches using daft: {error}"
            )
            raise
