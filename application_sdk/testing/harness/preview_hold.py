"""Hold a passing e2e run's connection for review, and reap it once it expires.

A full-DAG e2e purges its connection in teardown, about a minute after the
assertions, so the preview links a passing run writes for a reviewer point at
nothing by the time anyone opens them. When the run is asked to hold
(``E2E_PREVIEW_HOLD_HOURS``), teardown marks the connection with an expiry
instead of deleting it, and :func:`reap` — run on a schedule against each test
tenant — purges every marked connection whose expiry has passed.

Two properties keep this from leaking or deleting the wrong thing:

- **A connection is held only once it is marked.** If the mark cannot be
  written, teardown deletes as it always did; an unmarked connection is one the
  reaper would never find.
- **The reaper touches only the harness's own connections.** It purges a
  connection only when its description carries the hold marker AND its
  qualified name has the shape the harness mints (``default/<type>/<16
  digits>``), so a real connection that happens to mention the marker is never
  a target.

Usage (the scheduled reaper)::

    python -m application_sdk.testing.harness.preview_hold reap

reading ``ATLAN_BASE_URL`` and ``ATLAN_API_KEY`` (or the OAuth pair) from the
environment, the same way the harness does.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.identity import read_tenant_auth
from application_sdk.testing.harness.teardown import purge_connection

if TYPE_CHECKING:
    from pyatlan.client.aio.client import AsyncAtlanClient

logger = get_logger(__name__)

HOLD_ENV = "E2E_PREVIEW_HOLD_HOURS"
MAX_HOLD_HOURS = 72
MARKER = "e2e-preview-hold"

_EXPIRY_RE = re.compile(
    rf"\b{MARKER} expires=(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z)"
)
_EPHEMERAL_QN_RE = re.compile(r"^default/[a-z0-9_-]+/\d{16}$")


def hold_hours(environ: dict[str, str] | os._Environ[str]) -> int:
    """The requested hold, in hours: 0 when unset or unparseable, capped at
    :data:`MAX_HOLD_HOURS` so a typo cannot park test data for a month."""
    raw = (environ.get(HOLD_ENV) or "").strip()
    try:
        hours = int(raw)
    except ValueError:
        return 0
    return max(0, min(hours, MAX_HOLD_HOURS))


def stamp(moment: datetime) -> str:
    """*moment* as the UTC ``YYYY-MM-DDTHH:MM:SSZ`` a hold records."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hold_note(expires_at: datetime) -> str:
    """The description a held connection carries."""
    return f"{MARKER} expires={stamp(expires_at)}"


def held_until(description: str | None) -> datetime | None:
    """The expiry a description records, or ``None`` when it carries no hold."""
    found = _EXPIRY_RE.search(description or "")
    if not found:
        return None
    return datetime.strptime(found.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def is_ephemeral(qualified_name: str) -> bool:
    """Whether *qualified_name* is one the harness minted for a run."""
    return bool(_EPHEMERAL_QN_RE.match(qualified_name))


async def mark(
    client: AsyncAtlanClient,
    qualified_name: str,
    display_name: str,
    expires_at: datetime,
) -> None:
    """Mark a run's connection as held until *expires_at*.

    Raises:
        Exception: whatever pyatlan raises when the save is rejected. The
            caller deletes instead, so a failure here must not be swallowed.
    """
    from pyatlan.model.assets import Connection  # noqa: PLC0415

    held = Connection.updater(qualified_name=qualified_name, name=display_name)
    held.description = hold_note(expires_at)
    await client.asset.save(held)


async def expired(client: AsyncAtlanClient, now: datetime) -> list[str]:
    """Qualified names of harness connections whose hold has passed *now*."""
    from pyatlan.model.assets import Asset, Connection  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    request = (
        FluentSearch()
        .where(FluentSearch.asset_type(Connection))
        .where(FluentSearch.active_assets())
        .include_on_results(Asset.DESCRIPTION)
    ).to_request()
    due: list[str] = []
    async for asset in await client.asset.search(request):
        qn = asset.qualified_name or ""
        until = held_until(getattr(asset, "description", None))
        if until is not None and until <= now and is_ephemeral(qn):
            due.append(qn)
    return due


async def reap(client: AsyncAtlanClient, now: datetime | None = None) -> list[str]:
    """Purge every expired held connection; return the ones purged cleanly."""
    now = now or datetime.now(UTC)
    purged: list[str] = []
    for qn in await expired(client, now):
        report = await purge_connection(client, qn)
        if report.errors:
            logger.warning(
                "preview hold: purge of %s reported errors: %s", qn, report.errors
            )
            continue
        logger.info("preview hold: purged expired %s", qn)
        purged.append(qn)
    return purged


def expiry_from_now(hours: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=hours)


async def _reap_from_env() -> int:
    auth = read_tenant_auth(os.environ)
    async with atlas.atlas_client(
        auth.base_url,
        auth.api_key or "",
        oauth_client_id=auth.oauth_client_id,
        oauth_client_secret=auth.oauth_client_secret,
    ) as client:
        purged = await reap(client)
    logger.info("preview hold: reaped %d expired connection(s)", len(purged))
    return 0


def main(argv: Sequence[str]) -> int:
    if list(argv[1:]) != ["reap"]:
        logger.error(
            "usage: python -m application_sdk.testing.harness.preview_hold reap"
        )
        return 2
    return run_sync(_reap_from_env())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
