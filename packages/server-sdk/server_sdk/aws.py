"""AWS helpers for IAM-authenticated SQL connectors.

Behind the ``[aws]`` extra — ``boto3`` is imported lazily here and never in the
core. Shared by any AWS-hosted SQL source that authenticates with IAM (Redshift
today; RDS Postgres/MySQL when they move over). Connector-specific credential
calls (e.g. Redshift's ``get_cluster_credentials``) stay in the app; this module
owns only what is identical across AWS sources: region resolution, session /
client creation, the assume-role-across-regions loop, and URL assembly.
"""

from __future__ import annotations

import os
import re
from typing import Any, ClassVar

from server_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    InvalidInputError,
)
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

AWS_SESSION_NAME = os.getenv("AWS_SESSION_NAME", "temp-session")


# -- typed errors ------------------------------------------------------------


class AwsRegionNotFoundError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_AWS_REGION_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__(
            message="Could not determine AWS region from hostname",
            field="host",
            constraint="must contain an AWS region",
        )


class AwsCredentialSourceMissingError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_AWS_CREDENTIAL_SOURCE_MISSING"

    def __init__(self) -> None:
        super().__init__(
            message="No AWS credential source provided",
            field="credential_source",
            constraint="exactly one of session/temp_credentials/default",
        )


class AwsCredentialSourceConflictError(InvalidInputError):
    code: ClassVar[str] = "INVALID_INPUT_AWS_CREDENTIAL_SOURCE_CONFLICT"

    def __init__(self) -> None:
        super().__init__(
            message="Multiple AWS credential sources provided",
            field="credential_source",
            constraint="exactly one of session/temp_credentials/default",
        )


class AwsClientCreationError(DependencyUnavailableError):
    code: ClassVar[str] = "DEPENDENCY_UNAVAILABLE_AWS_CLIENT_CREATION"

    def __init__(self, *, service: str, cause: Exception) -> None:
        super().__init__(
            message=f"Failed to create AWS client for {service}",
            service=service,
            target="boto3.client",
            failure_reason=str(cause),
        )


# -- helpers -----------------------------------------------------------------


def get_region_name_from_hostname(hostname: str) -> str:
    """Extract an AWS region from an endpoint hostname, else raise."""
    match = re.search(r"\.([a-z]{2}-[a-z]+-\d)\.", hostname)
    if match:
        return match.group(1)
    match = re.search(r"-([a-z]{2}-[a-z]+-\d)\.", hostname)
    if match:
        return match.group(1)
    raise AwsRegionNotFoundError()


def create_aws_session(credentials: dict[str, Any]) -> Any:
    """A boto3 Session from ``aws_access_key_id``/``aws_secret_access_key``
    (falling back to ``username``/``password``)."""
    import boto3  # noqa: PLC0415 — optional dep: [aws]

    return boto3.Session(
        aws_access_key_id=credentials.get("aws_access_key_id")
        or credentials.get("username"),
        aws_secret_access_key=credentials.get("aws_secret_access_key")
        or credentials.get("password"),
    )


def create_aws_client(
    service: str,
    region: str,
    session: Any | None = None,
    temp_credentials: dict[str, str] | None = None,
    use_default_credentials: bool = False,
) -> Any:
    """A boto3 client from exactly one credential source."""
    sources = sum(
        [session is not None, temp_credentials is not None, use_default_credentials]
    )
    if sources == 0:
        raise AwsCredentialSourceMissingError()
    if sources > 1:
        raise AwsCredentialSourceConflictError()

    import boto3  # noqa: PLC0415 — optional dep: [aws]

    try:
        if session is not None:
            return session.client(service, region_name=region)  # type: ignore
        if temp_credentials is not None:
            return boto3.client(  # type: ignore
                service,
                aws_access_key_id=temp_credentials["AccessKeyId"],
                aws_secret_access_key=temp_credentials["SecretAccessKey"],
                aws_session_token=temp_credentials["SessionToken"],
                region_name=region,
            )
        return boto3.client(service, region_name=region)  # type: ignore
    except Exception as e:  # noqa: BLE001 — normalize to a typed error
        raise AwsClientCreationError(service=service, cause=e) from e


def get_all_aws_regions() -> list[str]:
    """All AWS regions via EC2 ``describe_regions``; hardcoded fallback on failure."""
    try:
        import boto3  # noqa: PLC0415 — optional dep: [aws]

        ec2 = boto3.client("ec2", region_name="us-east-1")
        return sorted(r["RegionName"] for r in ec2.describe_regions()["Regions"])
    except Exception:  # noqa: BLE001 — offline / no-EC2-perm fallback
        logger.warning(
            "Failed to retrieve AWS regions dynamically, using fallback list",
            exc_info=True,
        )
        return [
            "ap-northeast-1", "ap-south-1", "ap-southeast-1", "ap-southeast-2",
            "aws-global", "ca-central-1", "eu-central-1", "eu-north-1",
            "eu-west-1", "eu-west-2", "eu-west-3", "sa-east-1",
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
        ]  # fmt: skip


def assume_role_across_regions(
    role_arn: str,
    *,
    external_id: str | None = None,
    region_hint: str | None = None,
    session_name: str = "atlan_jdbc_metadata_extractor",
    duration_seconds: int = 3600,
) -> dict[str, str]:
    """``sts:AssumeRole`` retried across regions; returns temporary credentials.

    ``region_hint`` (if given and known) is tried first, then every other
    region. Raises :class:`AuthError` when no region succeeds.
    """
    regions = get_all_aws_regions()
    if region_hint:
        if region_hint in regions:
            regions.remove(region_hint)
        regions.insert(0, region_hint)

    import boto3  # noqa: PLC0415 — optional dep: [aws]

    kwargs: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
        "DurationSeconds": duration_seconds,
    }
    if external_id:
        kwargs["ExternalId"] = external_id

    for region in regions:
        try:
            logger.info("Assuming role in region %s", region)
            assumed = boto3.client("sts", region_name=region).assume_role(**kwargs)
            logger.info("Successfully assumed role in region %s", region)
            return assumed["Credentials"]
        except Exception:  # noqa: BLE001 — try the next region
            logger.info(
                "Error assuming role in region %s; trying others", region, exc_info=True
            )

    raise AuthError(
        message="Failed to assume role in any region",
        auth_method="iam_role",
        failure_reason="sts:AssumeRole failed across all regions",
    )


def create_engine_url(
    drivername: str,
    *,
    username: str,
    password: str,
    host: str,
    port: Any,
    database: str,
) -> str:
    """A SQLAlchemy connection URL with each part safely encoded.

    ``render_as_string(hide_password=False)`` is deliberate: bare ``str(url)``
    renders the password as ``***``, which would reach the driver as a literal
    broken password. ``URL.create`` percent-encodes each component.
    """
    from sqlalchemy.engine.url import URL  # noqa: PLC0415 — optional dep: [sql]

    return URL.create(
        drivername=drivername,
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    ).render_as_string(hide_password=False)
