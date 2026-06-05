# Generated from contract/app.pkl via contract-toolkit. DO NOT EDIT.
# Regenerate with: pkl eval -m . contract/app.pkl
from __future__ import annotations

from pydantic import Field

from application_sdk.testing.e2e.credential import CredentialBody


class FullFeaturedCredentialBody(CredentialBody):
    name: str = Field(alias="name")
    auth_type: str = Field(default="basic", alias="authType")
    host: str = Field(alias="host")
    port: int = Field(default=5432, alias="port")
    database: str = Field(alias="database")
    token_audience: str | None = Field(default=None, alias="token_audience")
    username: str = Field(default="", alias="username")
    password: str = Field(default="", alias="password")
    token: str = Field(default="", alias="token")
