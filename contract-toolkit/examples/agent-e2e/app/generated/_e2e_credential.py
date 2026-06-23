# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from pydantic import Field

from application_sdk.testing.e2e.credential import CredentialBody


class AgentE2eCredentialBody(CredentialBody):
    name: str = Field(alias="name")
    auth_type: str = Field(default="basic", alias="authType")
    connector_config_name: str = Field(
        default="atlan-connectors-agent-e2e", alias="connectorConfigName"
    )
    extra: dict = Field(default_factory=dict, alias="extra")
