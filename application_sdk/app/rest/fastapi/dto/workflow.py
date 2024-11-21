# Request/Response DTOs for workflows

from typing import Any, Dict

from pydantic import BaseModel


class TestAuthRequest(BaseModel):
    credential: Dict[str, Any]


class TestAuthResponse(BaseModel):
    success: bool
    message: str


class FetchMetadataRequest(BaseModel):
    credential: Dict[str, Any]


class FetchMetadataResponse(BaseModel):
    success: bool
    metadata: str


class PreflightCheckRequest(BaseModel):
    form_data: Dict[str, Any]


class PreflightCheckResponse(BaseModel):
    success: bool
    preflight_check: Dict[str, Any]


class StartWorkflowRequest(BaseModel):
    workflow_id: str
    input: Dict[str, Any]


class StartWorkflowResponse(BaseModel):
    workflow_id: str
    status: str
