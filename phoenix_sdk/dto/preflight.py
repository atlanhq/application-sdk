from pydantic import BaseModel, Field

from phoenix_sdk.dto.credentials import CredentialPayload


class FormData(BaseModel):
    include_filter: str = Field(..., alias="include-filter")
    exclude_filter: str = Field(..., alias="exclude-filter")
    temp_table_regex: str = Field(..., alias="temp-table-regex")

    class Config:
        populate_by_name = True


class PreflightPayload(BaseModel):
    credentials: CredentialPayload = Field(alias="credentials")
    form_data: FormData = Field(alias="formData")

    class Config:
        populate_by_name = True
