"""obstore credential-provider factories for Dapr binding auth modes that
obstore doesn't natively handle via static config keys.

``boto3`` and ``azure-identity`` are core SDK dependencies, so these factories
are always available regardless of which extras a connector installs.
"""

from __future__ import annotations

import os
from datetime import timezone
from typing import TYPE_CHECKING, Any

import boto3
from azure.identity import CertificateCredential
from botocore.exceptions import ClientError
from obstore.auth.azure import AzureCredentialProvider
from obstore.auth.boto3 import StsCredentialProvider

from application_sdk.common.aws_utils import _normalize_external_id

if TYPE_CHECKING:
    from typing import Unpack

    from obstore.auth.boto3 import AssumeRoleRequestRequestTypeDef
    from obstore.store import S3Credential

# Lazy import: a module-level get_logger() would create a circular import
# (observability -> storage -> observability), as in cloud.py.
_logger = None


def _log():
    global _logger
    if _logger is None:
        from application_sdk.observability.logger_adaptor import (  # noqa: PLC0415 — deferred to break circular import (observability ↔ storage)
            get_logger,
        )

        _logger = get_logger(__name__)
    return _logger


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


#: STS error codes that mean "this endpoint cannot serve this caller", so the
#: next endpoint may. ``InvalidClientTokenId`` is what STS in an opt-in region
#: returns when the caller's account has not enabled that region (or the base
#: credentials are a token that region does not accept). Any other error
#: (``AccessDenied``, a bad ``ExternalId``) is the same on every endpoint and
#: is raised at once.
_STS_ENDPOINT_FALLBACK_CODES = frozenset(
    {"InvalidClientTokenId", "RegionDisabledException"}
)


class _RegionFallbackStsCredentialProvider(_UtcExpiryStsCredentialProvider):
    """``AssumeRole`` against an ordered list of STS endpoints.

    Each session in *sessions* targets one STS endpoint (its region, or the
    global endpoint when it has none). The first is tried; the next only when
    the call fails with a code in ``_STS_ENDPOINT_FALLBACK_CODES``. The session
    that succeeds moves to the front, so a credential refresh calls it first.
    """

    def __init__(
        self,
        sessions: list[boto3.Session],
        **kwargs: Unpack[AssumeRoleRequestRequestTypeDef],
    ) -> None:
        super().__init__(sessions[0], **kwargs)
        self._sessions = list(sessions)

    def __call__(self) -> S3Credential:
        """Fetch credentials from the first STS endpoint that serves the call."""
        for index, session in enumerate(self._sessions):
            self.session = session
            try:
                credential = super().__call__()
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if (
                    code not in _STS_ENDPOINT_FALLBACK_CODES
                    or index == len(self._sessions) - 1
                ):
                    raise
                _log().warning(
                    "S3 assume-role: STS endpoint %s rejected the call (%s); "
                    "trying the next endpoint",
                    session.region_name or "global",
                    code,
                )
                continue
            if index:
                self._sessions.insert(0, self._sessions.pop(index))
                _log().info(
                    "S3 assume-role: using STS endpoint %s",
                    session.region_name or "global",
                )
            return credential
        raise AssertionError("unreachable: the last endpoint re-raises")


def make_s3_assume_role_provider(
    *,
    role_arn: str,
    session_name: str = "atlan-application-sdk",
    region: str | None = None,
    base_access_key: str | None = None,
    base_secret_key: str | None = None,
    base_session_token: str | None = None,
    external_id: str | None = None,
    fallback_region: str | None = None,
) -> Any:
    """Return an obstore S3CredentialProvider that calls STS::AssumeRole.

    Wraps ``obstore.auth.boto3.StsCredentialProvider``.

    When *base_access_key* / *base_secret_key* are supplied they are used as
    the base credentials for the STS call (e.g., an operator-supplied IAM
    user with ``sts:AssumeRole`` permission).  *base_session_token* is
    required when those base credentials are themselves temporary (STS-derived).
    When all three are omitted the boto3 session falls back to its default
    credential chain (instance profile, env vars, etc.) for the STS call.

    *external_id* is sent as ``ExternalId`` after surrounding whitespace is
    stripped; a ``None`` or blank value is omitted (see
    ``_normalize_external_id``).

    *fallback_region* is the bucket's region. It adds a second STS endpoint
    for when the session's own endpoint cannot serve the call:

    * The session has a region (*region*, ``AWS_DEFAULT_REGION``, config, or
      the pod's ``AWS_REGION``): STS is
      called there first, then in *fallback_region*.
    * The session has no region: STS is called in *fallback_region* first,
      then on the session default (the global endpoint). The global endpoint
      can issue a session token that S3 in an opt-in region rejects with
      ``InvalidToken``, while STS in an opt-in region the caller's account
      has not enabled rejects the call with ``InvalidClientTokenId``. Trying
      the bucket's region first and falling back covers both.

    The next endpoint is tried only on ``InvalidClientTokenId`` or
    ``RegionDisabledException``; the endpoint that works is kept for later
    refreshes. When *fallback_region* is unset or equals the session's region,
    the provider is the plain single-endpoint one.
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
    resolved_external_id = _normalize_external_id(external_id)
    if resolved_external_id:
        sts_kwargs["ExternalId"] = resolved_external_id

    if not fallback_region:
        return _UtcExpiryStsCredentialProvider(session, **sts_kwargs)

    # botocore reads AWS_DEFAULT_REGION and config, not AWS_REGION, so the
    # pod's AWS_REGION is applied here.
    if session.region_name is None and os.environ.get("AWS_REGION"):
        session = boto3.Session(
            **{**session_kwargs, "region_name": os.environ["AWS_REGION"]}
        )
    ambient_region = session.region_name
    if fallback_region == ambient_region:
        return _UtcExpiryStsCredentialProvider(session, **sts_kwargs)

    regional_session = boto3.Session(
        **{**session_kwargs, "region_name": fallback_region}
    )
    # With an ambient region (the pod's own region, or an explicit *region*)
    # its regional endpoint is tried first: a caller can always reach STS in
    # the region it runs in. Without one, the bucket's regional endpoint goes
    # first, ahead of the session default (the global endpoint), because the
    # global endpoint can issue a token that an opt-in region's S3 rejects.
    sessions = (
        [session, regional_session] if ambient_region else [regional_session, session]
    )
    return _RegionFallbackStsCredentialProvider(sessions, **sts_kwargs)


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
