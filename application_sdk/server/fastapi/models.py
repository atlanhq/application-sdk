# Request/Response DTOs for the FastAPI handler service.
#
# All auth/preflight/metadata/workflow request contracts live in
# application_sdk.handler.contracts — use those instead.

from pydantic import BaseModel, Field


class FileUploadResponse(BaseModel):
    """Response model for file upload endpoint matching heracles format.

    Field names use camelCase to stay consistent with the upstream
    Atlan ``/files`` endpoint contract.
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "977f156b-9c78-4bfc-bd74-f603f18c078a",
                "version": "weathered-firefly-9025",
                "isActive": True,
                "createdAt": 1764265919324,
                "updatedAt": 1764265919324,
                "fileName": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
                "rawName": "ddls_export.csv",
                "key": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
                "extension": ".csv",
                "contentType": "text/csv",
                "fileSize": 39144,
                "isEncrypted": False,
                "redirectUrl": "",
                "isUploaded": True,
                "uploadedAt": "2024-01-01T00:00:00Z",
                "isArchived": False,
            }
        }
    }

    id: str = Field(..., description="UUID of the file")
    version: str = Field(..., description="Human-readable version string")
    isActive: bool = Field(..., description="Whether the file is active")
    createdAt: int = Field(..., description="Unix timestamp in milliseconds")
    updatedAt: int = Field(..., description="Unix timestamp in milliseconds")
    fileName: str = Field(..., description="UUID + extension")
    rawName: str = Field(..., description="Original filename")
    key: str = Field(..., description="Object store key (uuid+extension)")
    extension: str = Field(..., description="File extension (e.g., '.csv')")
    contentType: str = Field(..., description="Content type of the file")
    fileSize: int = Field(..., description="File size in bytes")
    isEncrypted: bool = Field(..., description="Whether the file is encrypted")
    redirectUrl: str = Field(..., description="Redirect URL (empty for now)")
    isUploaded: bool = Field(..., description="Whether the file is uploaded")
    uploadedAt: str = Field(
        ..., description="ISO timestamp or '0001-01-01T00:00:00Z' default"
    )
    isArchived: bool = Field(..., description="Whether the file is archived")
