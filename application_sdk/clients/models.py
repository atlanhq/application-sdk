"""
Pydantic models for database client configurations.
This module provides Pydantic models for database connection configurations,
ensuring type safety and validation for database client settings.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """
    Pydantic model for database connection configuration.
    This model defines the structure for database connection configurations,
    including connection templates, required parameters, defaults, and additional
    connection parameters.
    """

    template: str = Field(
        ...,
        description="SQLAlchemy connection string template with placeholders for connection parameters",
    )
    required: List[str] = Field(
        default=[],
        description="List of required connection parameters that must be provided",
    )
    defaults: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Default connection parameters to be added to the connection string",
    )
    parameters: Optional[List[str]] = Field(
        default=None,
        description="List of additional connection parameter names that can be dynamically added from credentials to the connection string. ex: ['ssl_mode'] will be added to the connection string as ?ssl_mode=require",
    )
    connect_args: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional connection arguments to be passed to SQLAlchemy. ex: {'sslmode': 'require'}",
    )

    class Config:
        """Pydantic configuration for the DatabaseConfig model."""

        extra = "forbid"  # Prevent additional fields
        validate_assignment = True  # Validate on assignment
        use_enum_values = True  # Use enum values instead of enum objects
