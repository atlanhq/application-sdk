from enum import Enum
from typing import List

import pydantic


class DiffState(Enum):
    NEW = "NEW"
    DIFF = "DIFF"
    NO_DIFF = "NO_DIFF"
    DELETE = "DELETE"


class DiffProcessorConfig(pydantic.BaseModel):
    ignore_columns: List[str]
    num_partitions: int
    output_prefix: str
    chunk_size: int


class DiffOutputFormat(Enum):
    PARQUET = "parquet"


class TransformedDataRow(pydantic.BaseModel):
    type_name: str
    qualified_name: str
    record: str
