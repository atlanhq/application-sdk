"""Typed error leaves for the AWS utilities module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import (
    AppPermissionDeniedError,
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
)


@dataclass(kw_only=True)
class AwsAssumeRoleError(AppPermissionDeniedError):
    """Failed to assume an AWS IAM role via STS."""

    code: ClassVar[str] = "PERMISSION_AWS_ROLE"
    message: str = "Failed to assume AWS role"
    resource: str | None = "iam_role"
    required_action: str | None = "sts:AssumeRole"


@dataclass(kw_only=True)
class AwsRegionNotFoundError(InvalidInputError):
    """Could not extract a valid AWS region from the given hostname."""

    code: ClassVar[str] = "INVALID_INPUT_AWS_REGION"
    message: str = "Could not find valid AWS region from hostname"
    field: str | None = "hostname"


@dataclass(kw_only=True)
class AwsRdsTokenError(AuthError):
    """Failed to generate an RDS IAM authentication token."""

    code: ClassVar[str] = "AUTH_AWS_RDS_TOKEN"
    message: str = "Failed to get RDS auth token"
    service: str | None = "rds"


@dataclass(kw_only=True)
class AwsCredentialSourceMissingError(InvalidInputError):
    """No credential source was provided to create_aws_client."""

    code: ClassVar[str] = "INVALID_INPUT_AWS_CREDENTIAL_SOURCE_MISSING"
    message: str = "At least one credential source must be provided"
    field: str | None = "credential_source"


@dataclass(kw_only=True)
class AwsCredentialSourceConflictError(InvalidInputError):
    """More than one credential source was provided to create_aws_client."""

    code: ClassVar[str] = "INVALID_INPUT_AWS_CREDENTIAL_SOURCE_CONFLICT"
    message: str = "Only one credential source should be provided at a time"
    field: str | None = "credential_source"


@dataclass(kw_only=True)
class AwsClientCreationError(DependencyUnavailableError):
    """Failed to create an AWS service client."""

    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AWS_CLIENT"
    message: str = "Failed to create AWS client"
    service: str | None = "aws"
