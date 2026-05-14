"""Typed error leaves for the AWS utilities module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import AppPermissionDeniedError


@dataclass(kw_only=True)
class AwsAssumeRoleError(AppPermissionDeniedError):
    """Failed to assume an AWS IAM role via STS."""

    code: ClassVar[str] = "PERMISSION_AWS_ROLE"
    message: str = "Failed to assume AWS role"
    resource: str | None = "iam_role"
    required_action: str | None = "sts:AssumeRole"
