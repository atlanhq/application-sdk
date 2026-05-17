"""Reusable preflight check helpers for Handler implementations.

Mirrors the publish-check logic from marketplace-scripts/preflight/publish_checks.py,
ported to the SDK's async HTTP stack (httpx) and typed PreflightCheck contracts.

Connectors call these from ``preflight_check()`` (HTTP preflight) or from
:class:`~application_sdk.app.preflight.PublishPreflightMixin` (Temporal activity).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from application_sdk.handler.contracts import PreflightCheck
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_HERACLES_URL = os.environ.get(
    "ATLAN_INTERNAL_HERACLES_URL",
    os.environ.get("HERACLES_URL", "http://localhost:5201"),
)

_PERMISSIONS_TO_CHECK = ["ENTITY_CREATE", "ENTITY_UPDATE", "ENTITY_DELETE"]


# ---------------------------------------------------------------------------
# Internal helpers — mirror marketplace-scripts/preflight/publish_checks.py
# ---------------------------------------------------------------------------


async def _get_user(
    heracles_url: str,
    bearer_token: str,
    user_id: str,
) -> dict[str, Any]:
    """Fetch user record from Heracles GET /users/{user_id}."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{heracles_url.rstrip('/')}/users/{user_id}",
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise ValueError(
            f"GET /users/{user_id} returned {resp.status_code}: {resp.text}"
        )
    return resp.json()


async def _get_impersonation_token(
    token_url: str,
    client_id: str,
    client_secret: str,
    subject_token: str,
    user_id: str,
) -> str:
    """Exchange service token for a user-impersonation token via Keycloak token-exchange."""
    payload = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": client_id,
        "client_secret": client_secret,
        "subject_token": subject_token,
        "requested_subject": user_id,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=payload, timeout=10.0)
    if resp.status_code == 403:
        raise ValueError("User is disabled — token exchange rejected (403)")
    if resp.status_code != 200:
        raise ValueError(
            f"Token exchange failed {resp.status_code}: {resp.text}"
        )
    token = resp.json().get("access_token")
    if not token:
        raise ValueError(f"Token exchange response missing access_token: {resp.text}")
    return token


async def _check_entity_permissions(
    heracles_url: str,
    impersonation_token: str,
    connection_qualified_name: str,
) -> list[dict[str, Any]]:
    """Call Heracles POST /evaluates to check ENTITY_CREATE/UPDATE/DELETE."""
    entities = [
        {
            "action": perm,
            "entityId": f"{connection_qualified_name}/abc",
            "typeName": "Database",
        }
        for perm in _PERMISSIONS_TO_CHECK
    ]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{heracles_url.rstrip('/')}/evaluates",
            json={"entities": entities},
            headers={"Authorization": f"Bearer {impersonation_token}"},
            timeout=10.0,
        )
    if resp.status_code != 200:
        raise ValueError(f"POST /evaluates returned {resp.status_code}: {resp.text}")
    return resp.json()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


async def check_user_enabled(
    user_id: str,
    bearer_token: str,
    *,
    heracles_url: str = "",
) -> PreflightCheck:
    """Check whether the triggering user's account is active in Keycloak.

    Calls Heracles ``GET /users/{user_id}`` and inspects the ``enabled`` field.
    A disabled account will fail at the publish phase after extraction completes
    — catching it here wastes zero compute.

    Args:
        user_id: Keycloak user GUID of the workflow creator.
        bearer_token: Service bearer token with permission to read user records.
        heracles_url: Internal Heracles URL.  Falls back to
            ``ATLAN_INTERNAL_HERACLES_URL`` / ``HERACLES_URL`` env vars.

    Returns:
        A :class:`PreflightCheck` — ``passed=True`` if enabled, ``passed=False``
        with a clear message if disabled or if the lookup fails.
    """
    check_name = "UserEnabled"
    start = time.monotonic()
    url = heracles_url or _HERACLES_URL

    try:
        user = await _get_user(url, bearer_token, user_id)
        duration_ms = (time.monotonic() - start) * 1000

        if not user.get("enabled"):
            username = user.get("username") or user_id
            email = user.get("email", "")
            return PreflightCheck(
                name=check_name,
                passed=False,
                duration_ms=duration_ms,
                message=(
                    f"User account '{username}' ({email}) is disabled. "
                    "Contact your administrator to re-enable the account before running workflows."
                ),
            )

        return PreflightCheck(
            name=check_name,
            passed=True,
            duration_ms=duration_ms,
            message=f"User account '{user.get('username') or user_id}' is active.",
        )

    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        logger.warning("UserEnabled check failed user_id=%s error=%s", user_id, exc)
        return PreflightCheck(
            name=check_name,
            passed=False,
            duration_ms=duration_ms,
            message=f"Unable to verify user account status: {exc}",
        )


async def check_atlan_publish_permission(
    user_id: str,
    bearer_token: str,
    connection_qualified_name: str,
    *,
    heracles_url: str = "",
    token_url: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> list[PreflightCheck]:
    """Verify the triggering user can publish to the target Atlan connection.

    Runs two checks in parallel (mirroring marketplace-scripts PublishChecks):

    1. **UserEnabled** — ``GET /users/{user_id}`` confirms the account is active.
    2. **AtlanPublishPermission** — ``POST /evaluates`` with a user-impersonation
       token confirms ENTITY_CREATE / ENTITY_UPDATE / ENTITY_DELETE on the
       connection.  Skipped when ``token_url`` / ``client_id`` / ``client_secret``
       are not provided (e.g. during HTTP-time preflight where impersonation
       credentials are unavailable).

    Args:
        user_id: Keycloak user GUID of the workflow creator.
        bearer_token: Service bearer token (used for user lookup and as
            ``subject_token`` in the Keycloak token exchange).
        connection_qualified_name: Atlan connection qualifiedName
            (e.g. ``default/snowflake/1234567890``).
        heracles_url: Internal Heracles URL.  Falls back to env vars.
        token_url: Keycloak token endpoint for token exchange.  Falls back to
            ``ATLAN_AUTH_URL`` env var.
        client_id: OAuth2 client ID for token exchange.
        client_secret: OAuth2 client secret for token exchange.

    Returns:
        List of :class:`PreflightCheck` results — always at least ``UserEnabled``,
        plus ``AtlanPublishPermission`` when impersonation credentials are available.
    """
    url = heracles_url or _HERACLES_URL
    t_url = token_url or os.environ.get("ATLAN_AUTH_URL", "")

    user_check, perm_check = await asyncio.gather(
        _run_user_check(url, bearer_token, user_id),
        _run_permission_check(
            url,
            bearer_token,
            user_id,
            connection_qualified_name,
            t_url,
            client_id,
            client_secret,
        ),
        return_exceptions=True,
    )

    results: list[PreflightCheck] = []
    if isinstance(user_check, PreflightCheck):
        results.append(user_check)
    if isinstance(perm_check, PreflightCheck):
        results.append(perm_check)
    return results


async def _run_user_check(
    heracles_url: str, bearer_token: str, user_id: str
) -> PreflightCheck:
    return await check_user_enabled(
        user_id, bearer_token, heracles_url=heracles_url
    )


async def _run_permission_check(
    heracles_url: str,
    bearer_token: str,
    user_id: str,
    connection_qualified_name: str,
    token_url: str,
    client_id: str,
    client_secret: str,
) -> PreflightCheck:
    check_name = "AtlanPublishPermission"
    start = time.monotonic()

    if not token_url or not client_id or not client_secret:
        return PreflightCheck(
            name=check_name,
            passed=True,
            duration_ms=0.0,
            message="Permission check skipped — impersonation credentials not available.",
        )

    try:
        impersonation_token = await _get_impersonation_token(
            token_url, client_id, client_secret, bearer_token, user_id
        )
        permissions = await _check_entity_permissions(
            heracles_url, impersonation_token, connection_qualified_name
        )
        duration_ms = (time.monotonic() - start) * 1000

        denied = [p for p in permissions if not p.get("allowed")]
        if denied:
            denied_actions = ", ".join(p.get("action", "?") for p in denied)
            return PreflightCheck(
                name=check_name,
                passed=False,
                duration_ms=duration_ms,
                message=(
                    f"User lacks permission(s) [{denied_actions}] on connection "
                    f"'{connection_qualified_name}'. "
                    "Ensure the user account has the required Atlan role."
                ),
            )

        return PreflightCheck(
            name=check_name,
            passed=True,
            duration_ms=duration_ms,
            message=f"User has ENTITY_CREATE/UPDATE/DELETE on '{connection_qualified_name}'.",
        )

    except ValueError as exc:
        duration_ms = (time.monotonic() - start) * 1000
        if "disabled" in str(exc).lower():
            return PreflightCheck(
                name=check_name,
                passed=False,
                duration_ms=duration_ms,
                message=(
                    "User account is disabled — token exchange rejected. "
                    "Contact your administrator to re-enable the account."
                ),
            )
        logger.warning(
            "AtlanPublishPermission check failed connection=%s error=%s",
            connection_qualified_name,
            exc,
        )
        return PreflightCheck(
            name=check_name,
            passed=False,
            duration_ms=duration_ms,
            message=f"Cannot verify publish permission: {exc}",
        )
