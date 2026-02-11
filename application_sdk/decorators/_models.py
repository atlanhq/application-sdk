"""Enums, Pydantic models, and constants for the automation activity decorator."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# =============================================================================
# Enums
# =============================================================================


class ActivityCategory(str, Enum):
    """Category of an automation activity."""

    DATA = "Data"
    FLOW = "Flow"
    PROCESSING = "Processing"
    OUTPUT = "Output"
    UTILITY = "Utility"


class SubType(Enum):
    """Subtype annotation for activity parameters."""

    FILE_PATH = "file_path"
    SQL = "sql"
    ROUTES = "routes"


class ToolMetadata(BaseModel):
    """Optional metadata for a registered tool/activity."""

    icon: Optional[str] = Field(default=None, description="Icon for the tool")


# =============================================================================
# Constants
# =============================================================================

# JSON Schema extension key used by the automation engine UI
X_AUTOMATION_ENGINE = "x-automation-engine"

# Automation engine API endpoints
ENDPOINT_SERVER_READY = "/server/ready"
ENDPOINT_APPS = "/api/v1/apps"
ENDPOINT_TOOLS = "/api/v1/tools"

# Timeouts (seconds)
TIMEOUT_HEALTH_CHECK = 5.0
TIMEOUT_API_REQUEST = 30.0

# Default prefix for computing app qualified names
APP_QUALIFIED_NAME_PREFIX = "default/apps/"

# Environment variable for the automation engine API URL
ENV_AUTOMATION_ENGINE_API_URL = "ATLAN_AUTOMATION_ENGINE_API_URL"


# =============================================================================
# Pydantic models
# =============================================================================


class AppSpec(BaseModel):
    """App specification for SDK registration."""

    name: str = Field(description="Name of the app")
    qualified_name: Optional[str] = Field(
        default=None, description="Qualified name of the app"
    )
    task_queue: str = Field(description="Task queue for the app's workers")
    description: Optional[str] = Field(
        default=None, description="Description of the app"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None, description="App metadata"
    )


class Annotation(BaseModel):
    """Annotation for an activity parameter (display hints for the UI)."""

    sub_type: Optional[SubType] = Field(
        default=None,
        description="Subtype of the parameter",
    )
    display_name: str = Field(description="Display name of the parameter")


class Parameter(BaseModel):
    """Describes a single input or output parameter of an activity."""

    name: str
    description: str
    annotations: Annotation
    schema_extra: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional JSON Schema constraints (minItems, maxItems, minLength, maxLength, minimum, maximum, pattern, etc.)",
    )


class ActivitySpec(BaseModel):
    """Collected metadata for a single decorated activity."""

    name: str = Field(description="Name of the activity")
    display_name: str = Field(description="Display name of the activity")
    description: str = Field(description="Description of the activity")
    input_schema: Dict[str, Any] = Field(
        description="Input schema as JSON Schema dictionary"
    )
    output_schema: Dict[str, Any] = Field(
        description="Output schema as JSON Schema dictionary"
    )
    category: ActivityCategory = Field(description="Category of the activity")
    examples: Optional[List[str]] = Field(
        default=None, description="Examples of usage"
    )
    metadata: Optional[ToolMetadata] = Field(
        default=None, description="Tool metadata"
    )
