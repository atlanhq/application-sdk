"""Base protocol interface defining the 4 core methods.

Every protocol implements these 4 methods:
- apply(): Add auth to HTTP request
- materialize(): Return SDK-ready credentials
- refresh(): Refresh expired tokens
- validate(): Validate credential fields
"""

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from application_sdk.credentials.results import (
    ApplyResult,
    MaterializeResult,
    ValidationResult,
)
from application_sdk.credentials.types import FieldSpec


class BaseProtocol(ABC):
    """Abstract base class for all authentication protocols.

    Every protocol implements 4 core methods that handle different
    aspects of credential usage:

    - apply(): Called for every HTTP request via ctx.http
    - materialize(): Called by ctx.credentials.materialize()
    - refresh(): Called automatically before expiry or on 401
    - validate(): Called on credential load

    Subclasses should override default_fields to specify the fields
    required by the protocol, and default_config for protocol-specific
    configuration.

    Example:
        >>> class MyProtocol(BaseProtocol):
        ...     default_fields = [
        ...         FieldSpec(name="api_key", sensitive=True, required=True)
        ...     ]
        ...
        ...     def apply(self, credentials, request_info):
        ...         return ApplyResult(
        ...             headers={"Authorization": f"Bearer {credentials['api_key']}"}
        ...         )
        ...
        ...     def materialize(self, credentials):
        ...         return MaterializeResult(credentials={"api_key": credentials["api_key"]})
    """

    # Default fields for this protocol (can be overridden via FieldSpec)
    default_fields: List[FieldSpec] = []

    # Protocol-specific configuration defaults
    default_config: Dict[str, Any] = {}

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize protocol with optional configuration.

        Args:
            config: Protocol-specific configuration overrides.
                   Merged with default_config.
        """
        self.config = {**self.default_config, **(config or {})}

    @abstractmethod
    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Apply credentials to an HTTP request.

        Called for every HTTP request via ctx.http. Returns modifications
        to be applied to the outgoing request.

        Args:
            credentials: Resolved credential values.
            request_info: Request context containing:
                - method: HTTP method (GET, POST, etc.)
                - url: Target URL
                - headers: Current headers
                - body: Request body (if any)

        Returns:
            ApplyResult with headers, query_params, body modifications.

        Example:
            >>> def apply(self, credentials, request_info):
            ...     return ApplyResult(
            ...         headers={"Authorization": f"Bearer {credentials['api_key']}"}
            ...     )
        """
        raise NotImplementedError

    @abstractmethod
    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Format credentials for SDK usage.

        Called by ctx.credentials.materialize("name"). Returns credentials
        in a format suitable for external SDKs (boto3, snowflake-connector, etc.)

        Args:
            credentials: Resolved credential values.

        Returns:
            MaterializeResult with SDK-compatible credential dict.

        Example:
            >>> def materialize(self, credentials):
            ...     return MaterializeResult(credentials={
            ...         "aws_access_key_id": credentials["access_key_id"],
            ...         "aws_secret_access_key": credentials["secret_access_key"],
            ...         "region_name": credentials["region"]
            ...     })
        """
        raise NotImplementedError

    def refresh(self, credentials: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh expired tokens/sessions.

        Called automatically before expiry or on 401 response.
        Default implementation returns credentials unchanged (no refresh needed).

        Override this for protocols that support token refresh (OAuth, etc.)

        Args:
            credentials: Current credential values.

        Returns:
            Updated credentials dict with refreshed tokens.

        Example:
            >>> def refresh(self, credentials):
            ...     new_token = call_refresh_endpoint(credentials["refresh_token"])
            ...     return {**credentials, "access_token": new_token}
        """
        # Default: no refresh needed
        return credentials

    def validate(self, credentials: Dict[str, Any]) -> ValidationResult:
        """Validate credential fields.

        Called on credential load. Checks required fields and format.
        Default implementation validates based on default_fields.

        Args:
            credentials: Credential values to validate.

        Returns:
            ValidationResult with valid flag and errors/warnings.

        Example:
            >>> def validate(self, credentials):
            ...     if not credentials.get("api_key"):
            ...         return ValidationResult(
            ...             valid=False,
            ...             errors=["api_key is required"]
            ...         )
            ...     return ValidationResult(valid=True)
        """
        errors: List[str] = []
        warnings: List[str] = []

        # Check required fields from default_fields
        for field_spec in self.default_fields:
            value = credentials.get(field_spec.name)

            # Check required
            if field_spec.required and not value:
                errors.append(f"Required field '{field_spec.name}' is missing")
                continue

            if value:
                # Validate regex if provided
                if field_spec.validation_regex:
                    if not re.match(field_spec.validation_regex, str(value)):
                        errors.append(
                            f"Field '{field_spec.name}' does not match "
                            f"required pattern: {field_spec.validation_regex}"
                        )

                # Validate length constraints for strings
                if isinstance(value, str):
                    if field_spec.min_length and len(value) < field_spec.min_length:
                        errors.append(
                            f"Field '{field_spec.name}' must be at least "
                            f"{field_spec.min_length} characters"
                        )
                    if field_spec.max_length and len(value) > field_spec.max_length:
                        errors.append(
                            f"Field '{field_spec.name}' must be at most "
                            f"{field_spec.max_length} characters"
                        )

        return ValidationResult(
            valid=len(errors) == 0, errors=errors, warnings=warnings
        )

    def get_all_fields(
        self,
        field_overrides: Optional[Dict[str, FieldSpec]] = None,
        extra_fields: Optional[List[FieldSpec]] = None,
    ) -> List[FieldSpec]:
        """Get all fields including defaults, overrides, and extras.

        Combines default protocol fields with any customizations
        and additional fields specified by the credential declaration.

        Args:
            field_overrides: Dict of field name -> FieldSpec to override defaults.
            extra_fields: Additional fields beyond standard protocol fields.

        Returns:
            Combined list of all FieldSpec objects.
        """
        field_overrides = field_overrides or {}
        extra_fields = extra_fields or []

        # Start with default fields, applying any overrides
        result: List[FieldSpec] = []
        for default_field in self.default_fields:
            if default_field.name in field_overrides:
                # Use override but preserve defaults for unspecified attributes
                override = field_overrides[default_field.name]
                merged = FieldSpec(
                    name=override.name,
                    display_name=override.display_name or default_field.display_name,
                    placeholder=override.placeholder or default_field.placeholder,
                    help_text=override.help_text or default_field.help_text,
                    required=(
                        override.required
                        if override.required is not None
                        else default_field.required
                    ),
                    sensitive=(
                        override.sensitive
                        if override.sensitive is not None
                        else default_field.sensitive
                    ),
                    field_type=override.field_type or default_field.field_type,
                    options=override.options or default_field.options,
                    default_value=(
                        override.default_value
                        if override.default_value is not None
                        else default_field.default_value
                    ),
                    validation_regex=(
                        override.validation_regex or default_field.validation_regex
                    ),
                    min_length=(
                        override.min_length
                        if override.min_length is not None
                        else default_field.min_length
                    ),
                    max_length=(
                        override.max_length
                        if override.max_length is not None
                        else default_field.max_length
                    ),
                )
                result.append(merged)
            else:
                result.append(default_field)

        # Add extra fields at the end
        result.extend(extra_fields)

        return result

    def get_fields_schema(
        self,
        field_overrides: Optional[Dict[str, FieldSpec]] = None,
        extra_fields: Optional[List[FieldSpec]] = None,
    ) -> List[Dict[str, Any]]:
        """Get all fields as serializable dictionaries.

        Used for generating configmap/UI schema.

        Args:
            field_overrides: Dict of field name -> FieldSpec to override defaults.
            extra_fields: Additional fields beyond standard protocol fields.

        Returns:
            List of field dictionaries suitable for JSON serialization.
        """
        all_fields = self.get_all_fields(field_overrides, extra_fields)
        return [field.to_dict() for field in all_fields]
