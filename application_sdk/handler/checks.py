"""Reusable preflight check helpers for Handler implementations.

Connectors call these from ``preflight_check()`` to validate platform-side
publish prerequisites before the extraction pipeline runs.

Each helper returns a :class:`~application_sdk.handler.contracts.PreflightCheck`
that can be collected into a :class:`~application_sdk.handler.contracts.PreflightOutput`.
"""

from __future__ import annotations

import time

from application_sdk.handler.contracts import PreflightCheck
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


async def check_atlan_publish_permission(
    atlan_client: object,
    connection_qualified_name: str,
) -> PreflightCheck:
    """Verify the triggering user can write to the target Atlan connection.

    A 401 response means the user account is disabled or the session token is
    no longer valid.  A 403 means the account is active but lacks the Atlan
    role required to publish to this connection.  Both are caught here instead
    of after a full extraction run.

    Args:
        atlan_client: An ``AsyncAtlanClient`` (pyatlan_v9) configured with the
            triggering user's credentials.
        connection_qualified_name: The Atlan ``qualifiedName`` of the target
            connection (e.g. ``default/snowflake/1234567890``).

    Returns:
        A :class:`PreflightCheck` that is ``passed=True`` when the user has
        write access, or ``passed=False`` with a human-readable message when
        access is denied.
    """
    check_name = "AtlanPublishPermission"
    start = time.monotonic()

    try:
        from pyatlan_v9.model.assets import (
            Connection,  # type: ignore[import]
        )

        conn = await atlan_client.asset.get_by_qualified_name(  # type: ignore[attr-defined]
            qualified_name=connection_qualified_name,
            asset_type=Connection,
        )
        duration_ms = (time.monotonic() - start) * 1000

        if conn is None:
            return PreflightCheck(
                name=check_name,
                passed=False,
                duration_ms=duration_ms,
                message=(
                    f"Connection '{connection_qualified_name}' not found in Atlan. "
                    "Verify the connection exists and the user has access to it."
                ),
            )

        return PreflightCheck(
            name=check_name,
            passed=True,
            duration_ms=duration_ms,
            message=f"User has access to connection '{connection_qualified_name}'.",
        )

    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)

        if status_code == 401:
            msg = (
                "User account appears to be disabled or the session token is "
                "no longer valid (401 from Atlan). "
                "Contact your administrator to re-enable the account."
            )
        elif status_code == 403:
            msg = (
                f"User does not have permission to publish to "
                f"'{connection_qualified_name}' (403 from Atlan). "
                f"Ensure the user account has the required Atlan role."
            )
        else:
            msg = (
                f"Cannot verify publish permission for '{connection_qualified_name}': "
                f"{exc}. Ensure the user account is active and has the required Atlan role."
            )

        logger.warning(
            "AtlanPublishPermission check failed connection=%s status_code=%s error=%s",
            connection_qualified_name,
            status_code,
            exc,
        )
        return PreflightCheck(
            name=check_name,
            passed=False,
            duration_ms=duration_ms,
            message=msg,
        )
