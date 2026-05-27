"""obstore credential-provider factories for Dapr binding auth modes that
obstore doesn't natively handle via static config keys.

``boto3`` and ``azure-identity`` are core SDK dependencies, so these factories
are always available regardless of which extras a connector installs.
"""

from __future__ import annotations

from typing import Any

import boto3
from azure.identity import CertificateCredential
from obstore.auth.azure import AzureCredentialProvider
from obstore.auth.boto3 import StsCredentialProvider


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

    When *base_access_key* / *base_secret_key* are supplied they are used as
    the base credentials for the STS call (e.g., an operator-supplied IAM
    user with ``sts:AssumeRole`` permission).  When omitted the boto3 session
    uses its own default credential chain (instance profile, env vars, etc.)
    for the STS call.
    """
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

    Supply exactly one of *certificate_path* (path to a PFX/PEM file) or
    *certificate_data* (PEM bytes — see ``azureCertificate`` in the Dapr component
    YAML; for binary PFX use ``azureCertificateFile`` instead).
    """
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
