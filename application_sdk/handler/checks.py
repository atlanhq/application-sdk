"""Thin async client for Heracles' consolidated user-publish-check (HYP-829).

The user-publish-check — (1) the workflow creator's account is enabled and
(2) the creator may publish to the target connection — is performed
**server-side by Heracles** at ``POST /workflows/preflight/user-publish-check``.

The SDK no longer performs the user lookup, Keycloak token-exchange, or the
``/evaluates`` call itself: that logic now lives in one place (Heracles), as
agreed in the "Fixing preflights" design thread. This module is just the
client the :class:`~application_sdk.app.preflight.PublishPreflightMixin`
activity uses to call that endpoint.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_PREFLIGHT_PATH = "/workflows/preflight/user-publish-check"


def _default_heracles_url() -> str:
    """Resolve the internal Heracles URL at call time.

    Reading env vars at call time (rather than at import) lets deployment
    configs that inject ``ATLAN_INTERNAL_HERACLES_URL`` / ``HERACLES_URL``
    after the module loads take effect.
    """
    return os.environ.get(
        "ATLAN_INTERNAL_HERACLES_URL",
        os.environ.get("HERACLES_URL", "http://localhost:5201"),
    )


async def check_user_publish_preflight(
    user_id: str,
    connection_qualified_name: str,
    bearer_token: str,
    *,
    heracles_url: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call Heracles' consolidated user-publish-check.

    Args:
        user_id: Keycloak user GUID of the workflow creator.
        connection_qualified_name: Atlan connection qualifiedName
            (e.g. ``default/snowflake/1234567890``). May be empty — Heracles
            then runs only the user-enabled check.
        bearer_token: Service bearer token authenticating the SDK to Heracles.
        heracles_url: Internal Heracles URL. Falls back to
            ``ATLAN_INTERNAL_HERACLES_URL`` / ``HERACLES_URL`` env vars.
        timeout: Per-request timeout in seconds.

    Returns:
        Parsed response::

            {"passed": bool, "failed_checks": list[str], "message": str}

    Raises:
        ValueError: On a non-200 response (caller decides whether to fail open).
        httpx.HTTPError: On a transport-level failure.
    """
    url = (heracles_url or _default_heracles_url()).rstrip("/") + _PREFLIGHT_PATH
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={
                "user_id": user_id,
                "connection_qualified_name": connection_qualified_name,
            },
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=timeout,
        )
    if resp.status_code != 200:
        raise ValueError(
            f"user-publish-check returned {resp.status_code}: {resp.text}"
        )
    return resp.json()
