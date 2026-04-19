"""Git-specific credential types."""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict

from application_sdk.credentials.types import (
    BearerTokenCredential,
    CertificateCredential,
)


class GitSshCredential(CertificateCredential, frozen=True):
    """SSH key credential for Git operations.

    Inherits ``key_data`` (SSH private key) and ``passphrase`` from
    ``CertificateCredential``. The ``cert_data`` and ``ca_data`` fields
    are unused but present for protocol compatibility.
    """

    model_config = ConfigDict(frozen=True)

    @property
    def credential_type(self) -> str:
        return "git_ssh"

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.key_data:
            raise CredentialValidationError(
                "GitSshCredential.key_data (SSH private key) must not be empty",
                credential_name="git_ssh",
            )


class GitTokenCredential(BearerTokenCredential, frozen=True):
    """HTTPS PAT / deploy-token credential for Git operations.

    Inherits ``token`` from ``BearerTokenCredential``.
    """

    model_config = ConfigDict(frozen=True)

    @property
    def credential_type(self) -> str:
        return "git_token"

    async def validate(self) -> None:
        from application_sdk.credentials.errors import CredentialValidationError

        if not self.token:
            raise CredentialValidationError(
                "GitTokenCredential.token must not be empty",
                credential_name="git_token",
            )


# ---------------------------------------------------------------------------
# Parser functions
# ---------------------------------------------------------------------------


def _parse_git_ssh(data: dict[str, Any]) -> GitSshCredential:
    return GitSshCredential(
        key_data=data.get("key_data", ""),
        passphrase=data.get("passphrase", ""),
        cert_data=data.get("cert_data", ""),
        ca_data=data.get("ca_data", ""),
    )


def _parse_git_token(data: dict[str, Any]) -> GitTokenCredential:
    return GitTokenCredential(
        token=data.get("token", ""),
        expires_at=data.get("expires_at", ""),
    )
