"""obstore credential-provider factories for Dapr binding auth modes that
obstore doesn't natively handle via static config keys.

``boto3`` and ``azure-identity`` are core SDK dependencies, so these factories
are always available regardless of which extras a connector installs.
"""

from __future__ import annotations

from datetime import timezone
from typing import TYPE_CHECKING, Any

import boto3
from azure.identity import CertificateCredential
from obstore.auth.azure import AzureCredentialProvider
from obstore.auth.boto3 import StsCredentialProvider

if TYPE_CHECKING:
    from obstore.store import S3Credential


class _UtcExpiryStsCredentialProvider(StsCredentialProvider):
    """``StsCredentialProvider`` that normalises the STS expiry to stdlib UTC.

    obstore's Rust binding requires ``expires_at`` to carry exactly
    ``datetime.timezone.utc``. botocore deserialises STS timestamps with
    dateutil, so a live AssumeRole response carries ``dateutil.tz.tzutc()``.
    obstore's Python-side validation only checks ``tzinfo is not None``, so the
    value passes there and is rejected at the Rust boundary::

        UnauthenticatedError: The operation lacked valid authentication
        credentials for path External AWS credential provider:
        ValueError: expected datetime.timezone.utc

    The failure is deterministic, so the retry budget is exhausted without
    progress. Only the AssumeRole path is affected — obstore's
    ``Boto3CredentialProvider`` builds its own expiry with
    ``datetime.now(timezone.utc)``.

    ``astimezone`` preserves the instant, so the credential's real lifetime is
    unchanged; only the ``tzinfo`` representation is converted.
    """

    def __call__(self) -> S3Credential:
        """Fetch credentials, with ``expires_at`` converted to stdlib UTC."""
        credential = super().__call__()
        expires_at = credential.get("expires_at")
        if expires_at is not None:
            credential["expires_at"] = expires_at.astimezone(timezone.utc)
        return credential


def make_s3_assume_role_provider(
    *,
    role_arn: str,
    session_name: str = "atlan-application-sdk",
    region: str | None = None,
    base_access_key: str | None = None,
    base_secret_key: str | None = None,
    base_session_token: str | None = None,
    external_id: str | None = None,
) -> Any:
    """Return an obstore S3CredentialProvider that calls STS::AssumeRole.

    Wraps ``obstore.auth.boto3.StsCredentialProvider``.

    When *base_access_key* / *base_secret_key* are supplied they are used as
    the base credentials for the STS call (e.g., an operator-supplied IAM
    user with ``sts:AssumeRole`` permission).  *base_session_token* is
    required when those base credentials are themselves temporary (STS-derived).
    When all three are omitted the boto3 session falls back to its default
    credential chain (instance profile, env vars, etc.) for the STS call.
    """
    session_kwargs: dict[str, Any] = {}
    if region:
        session_kwargs["region_name"] = region
    if base_access_key and base_secret_key:
        session_kwargs["aws_access_key_id"] = base_access_key
        session_kwargs["aws_secret_access_key"] = base_secret_key
        if base_session_token:
            session_kwargs["aws_session_token"] = base_session_token

    session = boto3.Session(**session_kwargs)

    sts_kwargs: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name,
    }
    if external_id:
        sts_kwargs["ExternalId"] = external_id

    return _UtcExpiryStsCredentialProvider(session, **sts_kwargs)


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


def make_gcs_adc_provider() -> Any:
    """Return an obstore GCS credential provider backed by google-auth (ADC).

    Wraps ``obstore.auth.google.GoogleCredentialProvider``, which resolves
    Application Default Credentials via ``google.auth.default()`` and mints a
    short-lived OAuth token. This is the GCS ADC / Workload-Identity path —
    notably it handles Workload Identity Federation ``external_account`` files
    (e.g. GitHub OIDC → GCP, GKE WI), which obstore's built-in GCS
    credential-file decoder cannot. Imported lazily so google-auth is only
    needed when the GCS ADC path is actually exercised.

    Credentials are scoped to ``cloud-platform``: ``google.auth.default()`` yields
    unscoped credentials, but the WIF ``external_account`` → service-account
    impersonation ``generateAccessToken`` call requires a non-empty scope (an
    unscoped request fails with HTTP 400 INVALID_ARGUMENT).
    """
    import google.auth  # noqa: PLC0415
    from obstore.auth.google import GoogleCredentialProvider  # noqa: PLC0415

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    return GoogleCredentialProvider(credentials=credentials)
