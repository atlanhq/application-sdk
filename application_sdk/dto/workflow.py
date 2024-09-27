from typing import Dict
from pydantic import BaseModel, Field

from application_sdk.dto.credentials import BasicCredential


class WorkflowConfig(BaseModel):
    workflowId: str
    credentialsGUID: str
    includeFilterStr: str = "{}"
    excludeFilterStr: str = "{}"
    tempTableRegexStr: str = "[]"
    outputType: str = "JSON"
    outputPrefix: str = "/tmp/metadata"
    outputPath: str = "/tmp/metadata/workflowId/runId"
    verbose: bool = False
    chunkSize: int = -1


class MetadataPayload(BaseModel):
    exclude_filter: str = Field(default="", alias="exclude-filter")
    include_filter: str = Field(default="", alias="include-filter")
    temp_table_regex: str = Field(default="", alias="temp-table-regex")
    advanced_config_strategy: str = Field(
        default="default", alias="advanced-config-strategy"
    )
    use_source_schema_filtering: str = Field(
        default="false", alias="use-source-schema-filtering"
    )
    use_jdbc_internal_methods: str = Field(
        default="true", alias="use-jdbc-internal-methods"
    )
    authentication: str
    extraction_method: str = Field(alias="extraction-method")

    class Config:
        populate_by_name = True


class ConnectionPayload(BaseModel):
    connection: str


class WorkflowRequestPayload(BaseModel):
    credentials: BasicCredential = Field(alias="credentials")
    connection: ConnectionPayload
    metadata: MetadataPayload

    class Config:
        populate_by_name = True


class ExtractionConfig(BaseModel):
    workflowConfig: WorkflowConfig
    typename: str
    query: str
