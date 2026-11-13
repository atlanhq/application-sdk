"""JSON <-> Parquet file conversion helpers.

.. deprecated:: 3.58.0
    Every public name here is deprecated and removed in v4.0. Nothing in the SDK
    or the connector fleet calls them. ``convert_data_files`` is ``async`` but
    runs pandas on the event loop, and the converters log and return ``None``
    on any error, so a failed conversion looks like a missing file. Read and
    write the files with pyarrow directly (``pyarrow.json.read_json`` /
    ``pyarrow.parquet.write_table``), off the loop via ``run_in_thread``.
"""

import warnings
from collections import namedtuple
from enum import Enum

from typing_extensions import deprecated

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Explicit so ``from ... import *`` still yields ``file_converter_registry``,
# which is now served by ``__getattr__`` rather than bound as a module global.
__all__ = [
    "ConvertFile",
    "FileType",
    "convert_data_files",
    "convert_json_to_parquet",
    "convert_parquet_to_json",
    "enum_register",
    "file_converter_registry",  # noqa: F822 — served by __getattr__ (PEP 562)
    # Listed only because the Symbol Removal Check reads __all__ as the
    # module's surface and this SDK function was importable from here before.
    "get_logger",
]


def _enum_register():
    registry = {}

    def add(name: str):
        def inner(fn):
            registry[name] = fn
            return fn

        return inner

    Register = namedtuple("Register", ["add", "registry"])
    return Register(add, registry)


@deprecated(
    "enum_register is deprecated; use a plain dict keyed by the enum instead "
    "— will be removed in v4.0."
)
def enum_register():
    """
    Helps us register custom function for enum values

    .. deprecated:: 3.58.0
        Use a plain ``dict``. Will be removed in v4.0.
    """
    return _enum_register()


_file_converter_registry = _enum_register()

#: name -> (replacement, why). Served by ``__getattr__`` below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "file_converter_registry": (
        "pyarrow.json.read_json and pyarrow.parquet.write_table",
        "the file_converter module has no callers and hides conversion errors",
    ),
}


def __getattr__(name: str) -> object:
    """Serve the deprecated registry once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _file_converter_registry


@deprecated(
    "FileType is deprecated; use pyarrow.json.read_json and "
    "pyarrow.parquet.write_table instead — will be removed in v4.0."
)
class FileType(Enum):
    JSON = "json"
    PARQUET = "parquet"


@deprecated(
    "ConvertFile is deprecated; use pyarrow.json.read_json and "
    "pyarrow.parquet.write_table instead — will be removed in v4.0."
)
class ConvertFile(Enum):
    JSON_TO_PARQUET = "json_to_parquet"
    PARQUET_TO_JSON = "parquet_to_json"


@deprecated(
    "convert_data_files is deprecated; use pyarrow.json.read_json and "
    "pyarrow.parquet.write_table instead — will be removed in v4.0."
)
async def convert_data_files(
    input_file_paths: list[str], output_file_type: FileType
) -> list[str]:
    """
    Convert the input files to the specified file type

    .. deprecated:: 3.58.0
        Use pyarrow directly. Will be removed in v4.0.

    Args:
        input_file_paths: List[str] - List of input file paths
        output_file_type: FileType - The file type to convert to
    Returns:
        List[str] - List of converted file paths
    """
    if not input_file_paths:
        return []
    input_file_type = input_file_paths[0].split(".")[-1]
    # Iterating the enum, rather than calling ConvertFile(...), keeps this
    # deprecated function from tripping its sibling's deprecation warning.
    key = f"{input_file_type}_to_{output_file_type.value}"
    convert_file = next((m for m in ConvertFile if m.value == key), None)
    if convert_file is None:
        raise ValueError(f"{key!r} is not a valid ConvertFile")
    converter_func = _file_converter_registry.registry.get(convert_file)
    if converter_func is None:
        from application_sdk.common.errors import (  # noqa: PLC0415
            FileConverterNotFoundError,
        )

        # The caller was already warned that this function is deprecated; do
        # not warn a second time for the (equally deprecated) error it raises.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            error = FileConverterNotFoundError(
                message=f"No converter found for file type: {convert_file}"
            )
        raise error
    converted_files = []
    for file in input_file_paths:
        converted_file = converter_func(file)
        if converted_file:
            converted_files.append(converted_file)

    return converted_files


def _convert_json_to_parquet(file_path: str) -> str | None:
    try:
        import pandas as pd  # noqa: PLC0415 — optional dep: pandas
        import pyarrow.parquet as pq  # noqa: PLC0415 — optional dep: pyarrow

        from application_sdk.common._arrow import (  # noqa: PLC0415
            arrow_table_from_pandas,
        )

        logger.info("Converting file to parquet: %s", file_path)
        df = pd.read_json(file_path, orient="records", lines=True)
        df = df.loc[:, ~df.where(df.astype(bool)).isna().all(axis=0)]
        parquet_file_path = file_path.replace(".json", ".parquet")
        # preserve_index=None and snappy are what DataFrame.to_parquet passed.
        pq.write_table(
            arrow_table_from_pandas(df, preserve_index=None),
            parquet_file_path,
            compression="snappy",
        )
        return parquet_file_path
    except Exception:
        logger.error("Error converting file to parquet: %s", file_path, exc_info=True)
        return None


def _convert_parquet_to_json(file_path: str) -> str | None:
    try:
        import pandas as pd  # noqa: PLC0415 — optional dep: pandas

        logger.info("Converting file to json: %s", file_path)
        df = pd.read_parquet(file_path)
        json_file_path = file_path.replace(".parquet", ".json")
        df.to_json(json_file_path, orient="records", lines=True)
        return json_file_path
    except Exception:
        logger.error("Error converting file to json: %s", file_path, exc_info=True)
        return None


_file_converter_registry.registry[ConvertFile.JSON_TO_PARQUET] = (
    _convert_json_to_parquet
)
_file_converter_registry.registry[ConvertFile.PARQUET_TO_JSON] = (
    _convert_parquet_to_json
)


@deprecated(
    "convert_json_to_parquet is deprecated; use pyarrow.json.read_json and "
    "pyarrow.parquet.write_table instead — will be removed in v4.0."
)
def convert_json_to_parquet(file_path: str) -> str | None:
    """Convert the downloaded files from json to parquet

    .. deprecated:: 3.58.0
        Use pyarrow directly. Will be removed in v4.0.
    """
    return _convert_json_to_parquet(file_path)


@deprecated(
    "convert_parquet_to_json is deprecated; use pyarrow.parquet.read_table "
    "instead — will be removed in v4.0."
)
def convert_parquet_to_json(file_path: str) -> str | None:
    """Convert the downloaded files from parquet to json

    .. deprecated:: 3.58.0
        Use pyarrow directly. Will be removed in v4.0.
    """
    return _convert_parquet_to_json(file_path)
