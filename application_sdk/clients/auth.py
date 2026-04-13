"""Auth strategy protocol for client authentication.

An AuthStrategy bridges typed credentials (from credentials/types.py)
to the parameters a client needs to connect. SQL clients use
``build_url_params`` and ``build_connect_args``; REST API clients use
``build_headers``. Both can use ``build_url_query_params``.

Strategies are stateless — all state lives in the Credential instance.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from application_sdk.credentials.types import Credential


@runtime_checkable
class AuthStrategy(Protocol):
    """Maps a typed Credential to client connection parameters.

    Implement this protocol to add a new authentication method.
    Register implementations on a client's ``AUTH_STRATEGIES`` class
    variable keyed by the credential type they handle.
    """

    credential_type: type[Credential]
    """The Credential subclass this strategy handles."""

    def build_url_params(self, credential: Credential) -> dict[str, str]:
        """Return params to substitute into a URL/connection-string template.

        SQL example: ``{"password": "s3cret"}``.
        REST example: ``{}`` (REST clients rarely template creds into URLs).
        """
        ...

    def build_connect_args(self, credential: Credential) -> dict[str, Any]:
        """Return driver-level ``connect_args`` for ``create_engine()``.

        Example: ``{"private_key": <decrypted_key_bytes>}`` for keypair auth.
        REST clients return ``{}``.
        """
        ...

    def build_url_query_params(self, credential: Credential) -> dict[str, str]:
        """Return extra query parameters appended to the connection/request URL.

        Example: ``{"authenticator": "oauth"}`` for Snowflake OAuth.
        """
        ...

    def build_headers(self, credential: Credential) -> dict[str, str]:
        """Return HTTP headers for REST API authentication.

        Example: ``{"Authorization": "Bearer eyJ..."}``.
        SQL clients ignore this.
        """
        ...
