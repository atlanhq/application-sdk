"""Shared authentication layer for all client types.

Provides the ``AuthenticatedClient`` base class that handles credential
storage, auth strategy resolution, and URL building.  Protocol-specific
clients (SQL, REST, Wire Protocol) extend this class and add their own
transport logic.

Hierarchy::

    ClientInterface (ABC)
           │
    AuthenticatedClient          ← this module
           │
    ┌──────┴──────┐
    BaseSQLClient  BaseClient    ← protocol-specific
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from application_sdk.clients import ClientInterface
from application_sdk.common.error_codes import ClientError

if TYPE_CHECKING:
    from application_sdk.clients.auth import AuthStrategy
    from application_sdk.credentials.types import Credential


class AuthenticatedClient(ClientInterface):
    """Base class for clients that authenticate via typed credentials.

    Subclass and set ``AUTH_STRATEGIES`` to declare supported auth methods.
    Then call ``_resolve_strategy`` / ``_build_url`` from your ``load``
    implementation.

    This class does **not** implement ``load()`` or ``close()`` — those
    are left to protocol-specific subclasses.
    """

    AUTH_STRATEGIES: Dict[type, "AuthStrategy"] = {}
    """Map of Credential subclass → AuthStrategy instance.

    Example::

        AUTH_STRATEGIES = {
            BasicCredential: BasicAuthStrategy(),
            CertificateCredential: KeypairAuthStrategy(),
        }
    """

    credentials: Dict[str, Any] = {}

    def __init__(self, credentials: Dict[str, Any] | None = None) -> None:
        self.credentials = credentials or {}

    # ------------------------------------------------------------------
    # Strategy helpers
    # ------------------------------------------------------------------

    def _resolve_strategy(self, credential: "Credential") -> "AuthStrategy":
        """Look up the auth strategy for *credential*'s type.

        Raises:
            ClientError: If no strategy is registered for the credential type.
        """
        strategy = self.AUTH_STRATEGIES.get(type(credential))
        if strategy is None:
            raise ClientError(
                f"{ClientError.SQL_CLIENT_AUTH_ERROR}: "
                f"No auth strategy registered for {type(credential).__name__}. "
                f"Available: {[t.__name__ for t in self.AUTH_STRATEGIES]}"
            )
        return strategy

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def add_url_params(self, url: str, params: Dict[str, Any]) -> str:
        """Append query parameters to *url*.

        Works for both SQL connection strings
        (``postgresql://...?ssl=true``) and REST API URLs
        (``https://api.example.com?page_size=100``).
        """
        for key, value in params.items():
            if "?" not in url:
                url += "?"
            else:
                url += "&"
            url += f"{key}={value}"
        return url

    def _build_url(
        self,
        template: str,
        strategy: "AuthStrategy",
        credential: "Credential",
        defaults: Optional[Dict[str, Any]] = None,
        **connection_params: str,
    ) -> str:
        """Build a connection/base URL from a template, strategy, and params.

        1. Merges *connection_params* with ``strategy.build_url_params()``
           (strategy params win on key conflicts).
        2. Formats *template* with the merged params.
        3. Appends *defaults* as query parameters.
        4. Appends ``strategy.build_url_query_params()`` as query parameters.

        Returns:
            The fully built URL string.
        """
        url_params = {
            **connection_params,
            **strategy.build_url_params(credential),
        }
        url = template.format(**url_params)

        if defaults:
            url = self.add_url_params(url, defaults)

        query_params = strategy.build_url_query_params(credential)
        if query_params:
            url = self.add_url_params(url, query_params)

        return url
