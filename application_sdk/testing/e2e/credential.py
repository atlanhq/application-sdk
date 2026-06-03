"""Base class for codegen'd per-connector credential body models.

Each connector's ``contract/app.pkl`` generates a ``<Connector>CredentialBody``
subclass in ``app/generated/_e2e_credential.py`` via
``generateE2ECredentialPy()`` in ``contract-toolkit/src/App.pkl``. Fields are
derived from the pkl's
``credentialCommonFields + credentialAuthOptions[*].fields + extraFields``
(aligned with ``application_sdk.credentials.types.FieldSpec``). Aliases
use the AE-payload-body key names.

Connectors with ``hasCredentialConfig = false`` (e.g. openapi → public
source) do not emit this file; their ``_credential_body()`` returns ``None``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CredentialBody(BaseModel):
    """Base for codegen'd ``<Connector>CredentialBody`` classes.

    Subclasses extend with connector-specific fields driven by pkl's
    credential config schema. ``model_dump(by_alias=True)`` produces
    the exact dict injected into ``payload[].body`` on AE submit.
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        extra="allow",
    )
