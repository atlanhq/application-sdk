"""Typed error leaves for the storage formats module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    DependencyUnavailableError,
    InternalError,
    InvalidInputError,
    PreconditionError,
    UnimplementedError,
)


@dataclass(kw_only=True)
class AbstractFormatReaderError(UnimplementedError):
    """A Reader abstract method was called without a concrete implementation."""

    code: ClassVar[str] = "UNIMPLEMENTED_FORMAT_ABSTRACT"
    message: str = "Reader method not implemented by subclass"


@dataclass(kw_only=True)
class WriterClosedError(PreconditionError):
    """Write was attempted on a closed writer."""

    code: ClassVar[str] = "PRECONDITION_FORMAT_WRITER_CLOSED"
    message: str = "Cannot write to a closed writer"
    resource: str | None = "writer"


@dataclass(kw_only=True)
class ReaderClosedError(PreconditionError):
    """Read was attempted on a closed reader."""

    code: ClassVar[str] = "PRECONDITION_FORMAT_READER_CLOSED"
    message: str = "Cannot read from a closed reader"
    resource: str | None = "reader"


@dataclass(kw_only=True)
class UnsupportedDataframeTypeError(UnimplementedError):
    """The requested dataframe type is not supported by this writer/reader."""

    code: ClassVar[str] = "UNIMPLEMENTED_FORMAT_DATAFRAME_TYPE"
    message: str = "Unsupported dataframe type"
    observed_type: str | None = None


@dataclass(kw_only=True)
class UnsupportedDataTypeError(InvalidInputError):
    """Input data is not a supported type for conversion to a DataFrame."""

    code: ClassVar[str] = "INVALID_INPUT_FORMAT_DATA_TYPE"
    message: str = "Unsupported data type for DataFrame conversion"
    observed_type: str | None = None


@dataclass(kw_only=True)
class MissingStatisticsError(InternalError):
    """Writer statistics were expected but unavailable at close time."""

    code: ClassVar[str] = "INTERNAL_FORMAT_NO_STATISTICS"
    message: str = "No statistics data available"
    component: str | None = "writer"


@dataclass(kw_only=True)
class FormatPathRequiredError(InvalidInputError):
    """A required path argument was not provided to a format reader or writer."""

    code: ClassVar[str] = "INVALID_INPUT_FORMAT_PATH_REQUIRED"
    message: str = "path is required"
    field: str | None = "path"


@dataclass(kw_only=True)
class UnsupportedFileExtensionError(InvalidInputError):
    """The file extension is not supported by this format utility."""

    code: ClassVar[str] = "INVALID_INPUT_FORMAT_EXTENSION"
    message: str = "Unsupported file extension"
    field: str | None = "file_extension"
    observed_extension: str | None = None


@dataclass(kw_only=True)
class ObjectStoreReadError(DependencyUnavailableError):
    """Object store listing returned no files matching the expected extension.

    Surfaces the prefix that was searched so operators can immediately tell
    whether the upstream task wrote to the wrong path, was skipped entirely,
    or if the configured prefix on this read side is wrong.
    """

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_READ"
    message: str = (
        "No matching files found in object store — verify the upstream task "
        "wrote to this prefix and that the configured prefix is correct"
    )
    service: str | None = "object_store"
    path: str | None = None
    file_extension: str | None = None


@dataclass(kw_only=True)
class ObjectStoreDownloadError(DependencyUnavailableError):
    """No local files found and download from object store failed."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_OBJECT_STORE_DOWNLOAD"
    message: str = "No files found locally and failed to download from object store"
    service: str | None = "object_store"
    path: str | None = None
    file_extension: str | None = None


@dataclass(kw_only=True)
class SingleFilePathWithFileNamesError(InvalidInputError):
    """A single file path and file_names filter were both specified, which is invalid."""

    code: ClassVar[str] = "INVALID_INPUT_FORMAT_SINGLE_PATH_WITH_FILE_NAMES"
    message: str = "Cannot specify both a single file path and file_names filter"
    field: str | None = "file_names"
    path: str | None = None


@dataclass(kw_only=True)
class DaftNotInstalledError(DependencyUnavailableError):
    """daft is required but not installed."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_DAFT"
    message: str = "daft is not installed; install daft or use DataframeType.pandas"
    service: str | None = "daft"


@dataclass(kw_only=True)
class TempFolderPathMissingError(InternalError):
    """A write to a temp folder was attempted before the folder was initialised."""

    code: ClassVar[str] = "INTERNAL_FORMAT_TEMP_FOLDER_PATH_MISSING"
    message: str = "No temp folder path available"
    component: str | None = "parquet_writer"


@dataclass(kw_only=True)
class ReplacePrefixEmptyError(InvalidInputError):
    """replace_prefix=True was requested but the object-store prefix is empty."""

    code: ClassVar[str] = "INVALID_INPUT_FORMAT_REPLACE_PREFIX_EMPTY"
    message: str = "replace_prefix=True requires a non-empty object-store prefix"
    field: str | None = "path"


@dataclass(kw_only=True)
class FormatReadError(InternalError):
    """An unexpected error occurred while reading from a format reader."""

    code: ClassVar[str] = "INTERNAL_FORMAT_READ"
    message: str = "Error reading data"
    component: str | None = None


@dataclass(kw_only=True)
class FormatWriteError(InternalError):
    """An unexpected error occurred while writing via a format writer."""

    code: ClassVar[str] = "INTERNAL_FORMAT_WRITE"
    message: str = "Error writing dataframe"
    component: str | None = None


@dataclass(kw_only=True)
class FormatCloseError(InternalError):
    """An unexpected error occurred while closing a format writer."""

    code: ClassVar[str] = "INTERNAL_FORMAT_CLOSE"
    message: str = "Error closing writer"
    component: str | None = None


@dataclass(kw_only=True)
class FormatStatisticsWriteError(InternalError):
    """An unexpected error occurred while writing format statistics."""

    code: ClassVar[str] = "INTERNAL_FORMAT_STATISTICS_WRITE"
    message: str = "Error writing statistics"
    component: str | None = None
