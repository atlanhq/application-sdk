# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from application_sdk.testing.e2e.credential import CredentialBody
from pydantic import Field


class ExportCredentialBody(CredentialBody):
    name: str = Field(alias="name")
    auth_type: str = Field(default="api", alias="authType")
    password: str = Field(default="", alias="password")


class ExportAgentCredentialBody(CredentialBody):
    name: str = Field(alias="name")
    auth_type: str = Field(default="api", alias="authType")
    connector_config_name: str = Field(
        default="atlan-connectors-credential-configs", alias="connectorConfigName"
    )
    extra: dict = Field(default_factory=dict, alias="extra")
