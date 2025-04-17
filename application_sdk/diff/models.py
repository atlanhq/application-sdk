from enum import Enum
from typing import List, Optional

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
    entity: str


class PublishStateRow(pydantic.BaseModel):
    type_name: str
    qualified_name: str
    entity: str
    publish_status: str
    app_error_code: Optional[str]
    metastore_error_code: Optional[str]
    runbook_url: Optional[str]
