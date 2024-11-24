# Request/Response DTOs for workflows

from typing import Any, Dict, List

from pydantic import BaseModel, Field
from pydantic import BaseModel


class TestAuthRequest(BaseModel):
    credential: Dict[str, Any] = Field(
        description="A dictionary containing authentication credentials",
        min_items=1  # Ensures the dictionary is not empty
    )


class TestAuthResponse(BaseModel):
    success: bool
    message: str


class FetchMetadataRequest(BaseModel):
    credential: Dict[str, Any]


class FetchMetadataResponse(BaseModel):
    success: bool
    metadata: List[Dict[str, str]]


class PreflightCheckRequest(BaseModel):
    form_data: Dict[str, Any]


class PreflightCheckResponse(BaseModel):
    success: bool
    preflight_check: Dict[str, Any]


class StartWorkflowRequest(BaseModel):
    input: Dict[str, Any]


class StartWorkflowResponse(BaseModel):
    success: bool
