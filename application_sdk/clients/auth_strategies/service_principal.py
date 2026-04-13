"""Azure Service Principal auth strategy."""

from __future__ import annotations

from typing import Any

from application_sdk.credentials.types import Credential, ServicePrincipalCredential


class ServicePrincipalAuthStrategy:
    """Handles ServicePrincipalCredential for Azure clients.

    Creates an Azure ``ClientSecretCredential`` and returns it via
    ``build_connect_args`` so the client can store it for SDK calls.
    """

    credential_type = ServicePrincipalCredential

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        assert isinstance(credential, ServicePrincipalCredential)
        from azure.identity import ClientSecretCredential

        azure_credential = ClientSecretCredential(
            credential.tenant_id,
            credential.client_id,
            credential.client_secret,
        )
        return {"azure_credential": azure_credential}

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        return {}

    def build_headers(self, credential: Credential) -> dict[str, str]:
        return {}
