"""Base interfaces for unified IO in the Application SDK.

This module defines abstract base classes and common types for IO operations
that are shared across concrete reader and writer implementations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterator, List, Literal, Union

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Type aliases
DFType = Literal["pandas", "daft"]
BatchDF = Union[Iterator[Any], AsyncIterator[Any]]


@dataclass
class IOStats:
    """Statistics for IO write operations.

    Attributes:
        record_count: Total number of records written.
        chunk_count: Number of chunks written when using batched writes.
        file_paths: List of output file paths written.
        bytes_written: Total number of bytes written (if known).
    """

    record_count: int
    chunk_count: int
    file_paths: List[str]
    bytes_written: int


class Reader:
    """Abstract base class for all IO readers.

    Concrete readers must implement ``read`` and ``read_batches``.
    """

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        """Read the entire dataset and return a DataFrame.

        Args:
            *args: Positional arguments used by concrete readers.
            **kwargs: Keyword arguments used by concrete readers.

        Returns:
            Any: A pandas or daft DataFrame depending on the implementation.

        Raises:
            NotImplementedError: If the method is not implemented by subclass.
        """

        raise NotImplementedError

    async def read_batches(self, *args: Any, **kwargs: Any) -> BatchDF:
        """Return an iterator or async iterator of DataFrames.

        Args:
            *args: Positional arguments used by concrete readers.
            **kwargs: Keyword arguments used by concrete readers.

        Returns:
            BatchDF: Iterator or AsyncIterator yielding DataFrames.

        Raises:
            NotImplementedError: If the method is not implemented by subclass.
        """

        raise NotImplementedError

    async def close(self) -> None:
        """Perform resource cleanup for the reader.

        This default implementation does nothing. Subclasses may override
        it to release resources (e.g., connections, file handles).
        """

        return None


class Writer:
    """Abstract base class for all IO writers.

    Concrete writers must implement ``write`` and ``close``. The base class
    provides a default implementation of ``write_batches`` that iterates
    over the provided generator and calls ``write`` for each DataFrame.
    """

    async def write(self, dataframe: Any, *args: Any, **kwargs: Any) -> None:
        """Write a single DataFrame to the destination.

        Args:
            dataframe: The DataFrame to write (pandas or daft).
            *args: Positional arguments used by concrete writers.
            **kwargs: Keyword arguments used by concrete writers.

        Raises:
            NotImplementedError: If the method is not implemented by subclass.
        """

        raise NotImplementedError

    async def write_batches(
        self, batched_dataframe: BatchDF, *args: Any, **kwargs: Any
    ) -> None:
        """Write a stream of DataFrames to the destination.

        This default implementation iterates through the provided iterator
        or async iterator and delegates writing of each chunk to ``write``.

        Args:
            batched_dataframe: Iterator or AsyncIterator yielding DataFrames.
            *args: Positional arguments forwarded to ``write``.
            **kwargs: Keyword arguments forwarded to ``write``.

        Raises:
            Exception: Re-raises any exception that occurs during writing
                after logging the error for observability.
        """

        try:
            if hasattr(batched_dataframe, "__anext__"):
                async for dataframe in batched_dataframe:  # type: ignore[union-attr]
                    await self.write(dataframe, *args, **kwargs)
            else:
                for dataframe in batched_dataframe:  # type: ignore[union-attr]
                    await self.write(dataframe, *args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.error("Error occurred during batched write", exc_info=True)
            raise

    async def close(self) -> IOStats:
        """Finalize writes, upload artifacts if applicable, and return stats.

        Returns:
            IOStats: Aggregated statistics for the write operation.

        Raises:
            NotImplementedError: If the method is not implemented by subclass.
        """

        raise NotImplementedError
