"""Hold a passing e2e run's connection for review, and reap it once it expires.

A full-DAG e2e purges its connection in teardown, about a minute after the
assertions, so the preview links a passing run writes for a reviewer point at
nothing by the time anyone opens them. When the run is asked to hold
(``E2E_PREVIEW_HOLD_HOURS``), teardown marks the connection with an expiry
instead of deleting it, and :func:`reap` — run on a schedule against each test
tenant — purges every marked connection whose expiry has passed.

A hold belongs to the pull request that asked for it (``E2E_PREVIEW_PR``) and
to the suite that ran, so its lifetime follows the review rather than the clock
alone:

- **One hold per PR and suite.** A new run's hold replaces the previous one for
  the same PR and suite on that tenant, so re-running ``/e2e`` keeps one
  connection, not one per run, and the expiry restarts from the newest run.
- **Closing the PR ends the hold.** The reaper purges a hold whose PR is
  closed or merged without waiting for the expiry, which stays as the backstop
  for a PR that is left open.
- **A tenant holds at most** :data:`MAX_HOLDS_PER_TENANT` **at once.** Past
  that, a run deletes as usual rather than parking more test data.

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
environment, the same way the harness does, and ``GH_TOKEN`` (or
``GITHUB_TOKEN``) to read whether each hold's PR is still open. Without a token
the reaper purges on expiry only.
"""

from __future__ import annotations

import functools
import os
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.bridge import run_sync
from application_sdk.testing.harness.identity import read_tenant_auth
from application_sdk.testing.harness.teardown import purge_connection

if TYPE_CHECKING:
    from pyatlan.client.aio.client import AsyncAtlanClient

logger = get_logger(__name__)

HOLD_ENV = "E2E_PREVIEW_HOLD_HOURS"
PR_ENV = "E2E_PREVIEW_PR"
MAX_HOLD_HOURS = 72
MAX_HOLDS_PER_TENANT = 20
MARKER = "e2e-preview-hold"

_GITHUB_TIMEOUT_S = 20
_EXPIRY_RE = re.compile(
    rf"\b{MARKER}\b[^\n]*?\bexpires=(\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z)"
)
_PR = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+"
_PR_RE = re.compile(rf"^{_PR}$")
_NOTE_PR_RE = re.compile(rf"\bpr=({_PR})(?:\s|$)")
_SUITE = r"[A-Za-z0-9_.-]{1,80}"
_SUITE_RE = re.compile(rf"^{_SUITE}$")
_NOTE_SUITE_RE = re.compile(rf"\bsuite=({_SUITE})(?:\s|$)")
_EPHEMERAL_QN_RE = re.compile(r"^default/[a-z0-9_-]+/\d{16}$")


@dataclass(frozen=True)
class Hold:
    """One held connection, as its description records it."""

    qualified_name: str
    expires_at: datetime
    pr: str | None = None
    suite: str | None = None


def hold_hours(environ: dict[str, str] | os._Environ[str]) -> int:
    """The requested hold, in hours: 0 when unset or unparseable, capped at
    :data:`MAX_HOLD_HOURS` so a typo cannot park test data for a month."""
    raw = (environ.get(HOLD_ENV) or "").strip()
    try:
        hours = int(raw)
    except ValueError:
        return 0
    return max(0, min(hours, MAX_HOLD_HOURS))


def hold_owner(environ: dict[str, str] | os._Environ[str]) -> str | None:
    """The ``<owner>/<repo>#<number>`` PR that asked for the hold, or ``None``
    when unset or not of that shape."""
    raw = (environ.get(PR_ENV) or "").strip()
    return raw if _PR_RE.match(raw) else None


def stamp(moment: datetime) -> str:
    """*moment* as the UTC ``YYYY-MM-DDTHH:MM:SSZ`` a hold records."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hold_note(
    expires_at: datetime, pr: str | None = None, suite: str | None = None
) -> str:
    """The description a held connection carries. *pr* and *suite* are
    recorded only when they have the shape the reaper parses back."""
    parts = [MARKER]
    if pr and _PR_RE.match(pr):
        parts.append(f"pr={pr}")
    if suite and _SUITE_RE.match(suite):
        parts.append(f"suite={suite}")
    parts.append(f"expires={stamp(expires_at)}")
    return " ".join(parts)


def held_until(description: str | None) -> datetime | None:
    """The expiry a description records, or ``None`` when it carries no hold."""
    found = _EXPIRY_RE.search(description or "")
    if not found:
        return None
    return datetime.strptime(found.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def is_ephemeral(qualified_name: str) -> bool:
    """Whether *qualified_name* is one the harness minted for a run."""
    return bool(_EPHEMERAL_QN_RE.match(qualified_name))


def parse_hold(qualified_name: str, description: str | None) -> Hold | None:
    """The hold a connection carries, or ``None`` when it carries none or is
    not one the harness minted (and so is never the reaper's to touch)."""
    until = held_until(description)
    if until is None or not is_ephemeral(qualified_name):
        return None
    pr = _NOTE_PR_RE.search(description or "")
    suite = _NOTE_SUITE_RE.search(description or "")
    return Hold(
        qualified_name=qualified_name,
        expires_at=until,
        pr=pr.group(1) if pr else None,
        suite=suite.group(1) if suite else None,
    )


async def mark(
    client: AsyncAtlanClient,
    qualified_name: str,
    display_name: str,
    expires_at: datetime,
    *,
    pr: str | None = None,
    suite: str | None = None,
) -> None:
    """Mark a run's connection as held until *expires_at*.

    Raises:
        Exception: whatever pyatlan raises when the save is rejected. The
            caller deletes instead, so a failure here must not be swallowed.
    """
    from pyatlan.model.assets import Connection  # noqa: PLC0415

    held = Connection.updater(qualified_name=qualified_name, name=display_name)
    held.description = hold_note(expires_at, pr=pr, suite=suite)
    await client.asset.save(held)


async def holds(client: AsyncAtlanClient) -> list[Hold]:
    """Every hold on harness connections in the tenant."""
    from pyatlan.model.assets import Asset, Connection  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    request = (
        FluentSearch()
        .where(FluentSearch.asset_type(Connection))
        .where(FluentSearch.active_assets())
        .include_on_results(Asset.DESCRIPTION)
    ).to_request()
    found: list[Hold] = []
    async for asset in await client.asset.search(request):
        hold = parse_hold(
            asset.qualified_name or "", getattr(asset, "description", None)
        )
        if hold is not None:
            found.append(hold)
    return found


async def expired(client: AsyncAtlanClient, now: datetime) -> list[str]:
    """Qualified names of harness connections whose hold has passed *now*."""
    return [h.qualified_name for h in await holds(client) if h.expires_at <= now]


async def _purge_all(
    client: AsyncAtlanClient, qns: Sequence[str], why: str
) -> list[str]:
    """Purge each of *qns*; return the ones purged cleanly. A purge that
    reports errors is logged and left for the next reaper pass."""
    purged: list[str] = []
    for qn in qns:
        report = await purge_connection(client, qn)
        if report.errors:
            logger.warning(
                "preview hold: purge of %s reported errors: %s", qn, report.errors
            )
            continue
        logger.info("preview hold: purged %s (%s)", qn, why)
        purged.append(qn)
    return purged


async def hold(
    client: AsyncAtlanClient,
    qualified_name: str,
    display_name: str,
    expires_at: datetime,
    *,
    pr: str | None,
    suite: str | None,
    now: datetime | None = None,
) -> bool:
    """Hold a run's connection, replacing the PR's earlier hold for *suite*.

    Returns ``False``, holding nothing, when the tenant already carries
    :data:`MAX_HOLDS_PER_TENANT` other unexpired holds. The earlier hold is
    purged only after the new mark is saved, so a failed mark never leaves the
    PR with no hold at all; a failed purge of it is logged and left for the
    reaper, since its expiry still bounds it.

    Raises:
        Exception: whatever pyatlan raises when the mark is rejected, as
            :func:`mark` does.
    """
    now = now or datetime.now(UTC)
    current = [h for h in await holds(client) if h.qualified_name != qualified_name]
    superseded = [
        h.qualified_name
        for h in current
        if pr and suite and h.pr == pr and h.suite == suite
    ]
    others = [
        h for h in current if h.qualified_name not in superseded and h.expires_at > now
    ]
    if len(others) >= MAX_HOLDS_PER_TENANT:
        logger.info(
            "preview hold: tenant already holds %d connection(s); not holding %s",
            len(others),
            qualified_name,
        )
        return False
    await mark(client, qualified_name, display_name, expires_at, pr=pr, suite=suite)
    try:
        await _purge_all(client, superseded, f"superseded by {qualified_name}")
    # The new hold is saved; an earlier one left behind is bounded by its expiry.
    except Exception:  # noqa: BLE001
        logger.warning(
            "preview hold: could not purge the hold(s) %s replaced; the reaper "
            "purges them at expiry",
            superseded,
            exc_info=True,
        )
    return True


PrClosed = Callable[[set[str]], Awaitable[set[str]]]


async def closed_prs(
    prs: set[str],
    token: str,
    api_url: str = "https://api.github.com",
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> set[str]:
    """Which of *prs* (``<owner>/<repo>#<n>``) GitHub reports as closed.

    A PR whose state cannot be read counts as open: its hold then waits for
    its expiry, and the next hourly pass asks again.
    """
    closed: set[str] = set()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(
        base_url=api_url,
        headers=headers,
        timeout=_GITHUB_TIMEOUT_S,
        transport=transport,
    ) as gh:
        for pr in sorted(p for p in prs if _PR_RE.match(p)):
            repo, number = pr.split("#")
            try:
                response = await gh.get(f"/repos/{repo}/pulls/{int(number)}")
                response.raise_for_status()
                state = response.json().get("state")
            except (httpx.HTTPError, ValueError):
                logger.warning(
                    "preview hold: could not read the state of %s; keeping its "
                    "hold until it expires",
                    pr,
                    exc_info=True,
                )
                continue
            if state == "closed":
                closed.add(pr)
    return closed


async def reap(
    client: AsyncAtlanClient,
    now: datetime | None = None,
    *,
    pr_closed: PrClosed | None = None,
) -> list[str]:
    """Purge every hold that has expired or whose PR is closed; return the
    ones purged cleanly. Without *pr_closed*, expiry alone decides."""
    now = now or datetime.now(UTC)
    found = await holds(client)
    closed: set[str] = set()
    if pr_closed is not None:
        open_prs = {h.pr for h in found if h.pr and h.expires_at > now}
        closed = await pr_closed(open_prs) if open_prs else set()
    due = [
        h.qualified_name
        for h in found
        if h.expires_at <= now or (h.pr is not None and h.pr in closed)
    ]
    return await _purge_all(client, due, "hold expired or its PR closed")


def expiry_from_now(hours: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) + timedelta(hours=hours)


async def _reap_from_env() -> int:
    auth = read_tenant_auth(os.environ)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    pr_closed: PrClosed | None = None
    if token and api_url.startswith("https://"):
        pr_closed = functools.partial(closed_prs, token=token, api_url=api_url)
    else:
        logger.warning("preview hold: no GitHub token; holds are purged on expiry only")
    async with atlas.atlas_client(
        auth.base_url,
        auth.api_key or "",
        oauth_client_id=auth.oauth_client_id,
        oauth_client_secret=auth.oauth_client_secret,
    ) as client:
        purged = await reap(client, pr_closed=pr_closed)
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
