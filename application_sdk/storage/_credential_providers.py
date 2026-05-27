"""obstore credential-provider factories for Dapr binding auth modes that
obstore doesn't natively handle via static config keys.

All factories guard their optional-dependency imports and raise
StorageConfigError with install instructions if the dependency is absent.

Requires extras:
  - ``make_s3_assume_role_provider``: ``pip install atlan-application-sdk[iam_auth]``
  - ``make_azure_certificate_provider``: ``pip install atlan-application-sdk[azure]``
"""

from __future__ import annotations

from typing import Any


def make_s3_assume_role_provider(
    *,
    role_arn: str,
    session_name: str = "atlan-application-sdk",
    region: str | None = None,
    base_access_key: str | None = None,
    base_secret_key: str | None = None,
    external_id: str | None = None,
) -> Any:
    """Return an obstore S3CredentialProvider that calls STS::AssumeRole.

    Wraps ``obstore.auth.boto3.StsCredentialProvider``.
    Requires ``pip install atlan-application-sdk[iam_auth]``.

    When *base_access_key* / *base_secret_key* are supplied they are used as
    the base credentials for the STS call (e.g., an operator-supplied IAM
    user with ``sts:AssumeRole`` permission).  When omitted the boto3 session
    uses its own default credential chain (instance profile, env vars, etc.)
    for the STS call.
    """
    try:
        import boto3  # noqa: PLC0415
        from obstore.auth.boto3 import StsCredentialProvider  # noqa: PLC0415
    except ImportError as exc:
        from application_sdk.storage.errors import StorageConfigError  # noqa: PLC0415

        raise StorageConfigError(
            "assumeRoleArn requires boto3: pip install atlan-application-sdk[iam_auth]"
        ) from exc

    session_kwargs: dict[str, Any] = {}
    if region:
        session_kwargs["region_name"] = region
    if base_access_key and base_secret_key:
        session_kwargs["aws_access_key_id"] = base_access_key
        session_kwargs["aws_secret_access_key"] = base_secret_key

    session = boto3.Session(**session_kwargs)

    sts_kwargs: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
    }
    if external_id:
        sts_kwargs["ExternalId"] = external_id

    return StsCredentialProvider(session, **sts_kwargs)


def make_azure_certificate_provider(
    *,
    tenant_id: str,
    client_id: str,
    certificate_path: str | None = None,
    certificate_data: bytes | None = None,
    certificate_password: str | None = None,
    authority_host: str | None = None,
) -> Any:
    """Return an obstore AzureCredentialProvider backed by a certificate credential.

    Wraps ``azure.identity.CertificateCredential`` in
    ``obstore.auth.azure.AzureCredentialProvider``.
    Requires ``pip install atlan-application-sdk[azure]``.

    Supply exactly one of *certificate_path* (path to a PFX/PEM file) or
    *certificate_data* (PFX/PEM bytes).
    """
    try:
        from azure.identity import CertificateCredential  # noqa: PLC0415
        from obstore.auth.azure import AzureCredentialProvider  # noqa: PLC0415
    except ImportError as exc:
        from application_sdk.storage.errors import StorageConfigError  # noqa: PLC0415

        raise StorageConfigError(
            "Azure certificate auth requires: pip install atlan-application-sdk[azure]"
        ) from exc

    cred_kwargs: dict[str, Any] = {
        "tenant_id": tenant_id,
        "client_id": client_id,
    }
    if certificate_path:
        cred_kwargs["certificate_path"] = certificate_path
    if certificate_data:
        cred_kwargs["certificate_data"] = certificate_data
    if certificate_password:
        cred_kwargs["password"] = certificate_password
    if authority_host:
        cred_kwargs["authority"] = authority_host

    credential = CertificateCredential(**cred_kwargs)
    return AzureCredentialProvider(credential=credential)
