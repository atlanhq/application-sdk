from collections import namedtuple
from enum import Enum
from typing import List, Optional

import pandas as pd

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def enum_register():
    """
    Helps us register custom function for enum values
    """
    registry = {}

    def add(name: str):
        def inner(fn):
            registry[name] = fn
            return fn

        return inner

    Register = namedtuple("Register", ["add", "registry"])
    return Register(add, registry)


file_converter_registry = enum_register()


# Edit the enums here to add new file types
class FileType(Enum):
    JSON = "json"
    PARQUET = "parquet"


# Edit the enums here to add new file conversions
class ConvertFile(Enum):
    JSON_TO_PARQUET = "json_to_parquet"
    PARQUET_TO_JSON = "parquet_to_json"


async def convert_data_files(
    input_file_paths: List[str], output_file_type: FileType
) -> List[str]:
    """
    Convert the input files to the specified file type
    Args:
        input_file_paths: List[str] - List of input file paths
        output_file_type: FileType - The file type to convert to
    Returns:
        List[str] - List of converted file paths
    """
    if not input_file_paths:
        return []
    input_file_type = input_file_paths[0].split(".")[-1]
    convert_file = ConvertFile(f"{input_file_type}_to_{output_file_type.value}")
    converter_func = file_converter_registry.registry.get(convert_file)
    converted_files = []
    try:
        for file in input_file_paths:
            converted_file = converter_func(file)
            if converted_file:
                converted_files.append(converted_file)
    except KeyError:
        raise ValueError(f"No converter found for file type: {convert_file}")

    return converted_files


# Add the main logic here to convert the files here
@file_converter_registry.add(ConvertFile.JSON_TO_PARQUET)
def convert_json_to_parquet(file_path: str) -> Optional[str]:
    """Convert the downloaded files from json to parquet"""
    try:
        logger.info(f"Converting {file_path} to parquet")
        df = pd.read_json(file_path, orient="records", lines=True)
        df = df.loc[:, ~df.where(df.astype(bool)).isna().all(axis=0)]
        parquet_file_path = file_path.replace(".json", ".parquet")
        df.to_parquet(parquet_file_path)
        return parquet_file_path
    except Exception as e:
        logger.error(f"Error converting {file_path} to parquet: {e}")
        return None


@file_converter_registry.add(ConvertFile.PARQUET_TO_JSON)
def convert_parquet_to_json(file_path: str) -> Optional[str]:
    """Convert the downloaded files from parquet to json"""
    try:
        logger.info(f"Converting {file_path} to json")
        df = pd.read_parquet(file_path)
        json_file_path = file_path.replace(".parquet", ".json")
        df.to_json(json_file_path, orient="records", lines=True)
        return json_file_path
    except Exception as e:
        logger.error(f"Error converting {file_path} to json: {e}")
        return None
