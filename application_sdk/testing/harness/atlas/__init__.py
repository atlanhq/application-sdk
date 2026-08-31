"""Atlas reads, lifted out of ``testing/e2e/client.py``.

``client.py`` carried two unrelated concerns: talking to the Automation Engine,
and searching Atlas. Splitting it here and in
:mod:`application_sdk.testing.harness.automation_engine` is child F on FND-224.

Two things change on the way across, and neither is cosmetic.

**Async only, one client.** Five ``asyncio.run`` bridges lived in ``client.py``,
and each stood up a fresh event loop *and* a fresh ``AsyncAtlanClient`` — so a
fresh TLS handshake — per call. ``poll_atlas_for_connection`` did that on every
30-second iteration: up to ~50 loops and ~50 handshakes inside a single
1,500-second poll, for one boolean. Here the client is a parameter
(:func:`atlas_client` opens one), every read is ``async``, and the poll holds
one client for its whole window. The sync entry points that remain on
``AEWorkflowClient`` are one-liners over
:func:`~application_sdk.testing.harness.bridge.run_sync` (decision D1), which
also closes a gap none of the five had: an ``asyncio.run`` called from inside a
running loop raises a bare ``RuntimeError`` from deep in asyncio, where
``run_sync`` raises a typed leaf naming the ``_async`` twin to await instead.

**No fail-open counts.** All four count and sample methods returned zeros or
``[]`` on a search error. The visible symptom is not a silent pass but a
*misleading diagnosis*: the run reports "asset floor not met" and points the
reader at the connector when the real fault was Atlas. These readers return
:class:`~application_sdk.testing.harness.outcome.Indeterminate` instead, so an
unreadable count can no longer be graded as a low count. A type with no assets
is a ``0`` in a
:class:`~application_sdk.testing.harness.outcome.Settled` result; it is never
the same value as an unreadable search.

That distinction is only *available* here — it is not yet acted on. The sync
``AEWorkflowClient`` methods still collapse an unreadable read back to zeros,
because their callers in ``testing/e2e/base.py`` take ``dict[str, int]`` and
grade it; teaching those call sites the third answer is child H, which owns the
grading. Until then the collapse happens in exactly one place, named
:func:`~application_sdk.testing.e2e.client._settled_or_fail_open`, so what child
H deletes is one function rather than four scattered ``except`` blocks.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, TypeAlias, TypeVar, Union

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.harness.atlas._errors import UnknownConnectorTypeError
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import (
    Indeterminate,
    NeverStarted,
    Outcome,
    Settled,
)
from application_sdk.testing.harness.waiting import poll_until

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan is a lazy import
    from pyatlan.client.aio.client import AsyncAtlanClient

logger = get_logger(__name__)

T = TypeVar("T")

#: What a read that does not poll can answer: it got a number, or it could not
#: look. Narrower than
#: :data:`~application_sdk.testing.harness.outcome.Outcome` on purpose — a
#: single search has no budget to expire, no progress to stall and no start to
#: wait for, so the three verdicts that describe a *wait* are not among its
#: possible answers. Still an ``Outcome``, so a caller accumulating verdicts for
#: :func:`~application_sdk.testing.harness.outcome.grade` takes it unchanged.
#:
#: ``Union`` rather than ``|`` for the same reason ``Outcome`` uses it: the alias
#: is generic and has to stay subscriptable at runtime.
Reading: TypeAlias = Union[Settled[T], Indeterminate[T]]

__all__ = [
    "ADMIN_ROLE_NAME",
    "DEFAULT_TYPE_NAMES",
    "AdminIdentity",
    "Reading",
    "admin_identity",
    "atlas_client",
    "connection_exists",
    "count_assets",
    "count_lineage",
    "count_total_assets",
    "create_connection",
    "poll_for_connection",
    "sample_qualified_names",
    # Leaf
    "UnknownConnectorTypeError",
]

#: The built-in tenant role every harness-created Connection falls back to.
#: Atlas refuses a Connection with no non-empty admin list at all
#: (``ATLAS-400-00-114``), and this is the role that lets *any* tenant admin
#: manage the ephemeral connection rather than only the token that created it.
ADMIN_ROLE_NAME = "$admin"

#: Types a connector run counts by default. The five levels of the SQL asset
#: hierarchy, in the order a reader expects to see them.
DEFAULT_TYPE_NAMES: tuple[str, ...] = ("Database", "Schema", "Table", "View", "Column")


@asynccontextmanager
async def atlas_client(
    tenant_url: str,
    api_token: str,
    *,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
) -> AsyncIterator[AsyncAtlanClient]:
    """Open one ``AsyncAtlanClient`` for a batch of Atlas reads.

    Centralised so every pyatlan call (search, role_cache) goes through the same
    auth path. OAuth-client identity is preferred when both are present because
    OAuth tokens are explicitly scoped, whereas the API-key bearer often resolves
    to a broad-permissioned service account whose name confuses RBAC
    diagnostics — and a service account with realm-admin can be missing from an
    asset ACL the OAuth client *is* on, which is the entire reason the choice
    exists.

    Args:
        tenant_url: Base URL of the tenant.
        api_token: Bearer token, used when no OAuth pair is supplied.
        oauth_client_id: OAuth ``client_credentials`` id, with its secret.
        oauth_client_secret: OAuth ``client_credentials`` secret, with its id.

    Yields:
        The client, closed on exit. Hold it across a whole poll or a whole
        batch of per-type searches — that reuse is the point.
    """
    # Lazy: pyatlan is a heavy import; testing-time-only.
    from pyatlan.client.aio.client import AsyncAtlanClient  # noqa: PLC0415

    if oauth_client_id and oauth_client_secret:
        client = AsyncAtlanClient(
            base_url=tenant_url,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
        )
    else:
        client = AsyncAtlanClient(base_url=tenant_url, api_key=api_token)
    async with client as opened:
        yield opened


def _one_shot(label: str, started: float) -> tuple[str, int, timedelta]:
    """The three fields every outcome carries, for a read that does not poll.

    A single search is one attempt; its ``elapsed`` is still worth reporting,
    because "the count read took 40s" is the first clue that Atlas is the slow
    dependency rather than the connector.
    """
    return label, 1, timedelta(seconds=time.monotonic() - started)


def _unreadable(label: str, started: float, cause: BaseException) -> Indeterminate[T]:
    """Report a failed search as no verdict rather than as an empty result."""
    name, attempts, elapsed = _one_shot(label, started)
    logger.warning(
        "%s could not be read — reporting no verdict rather than an empty "
        "result, so an Atlas outage cannot be graded as a missing asset",
        name,
        exc_info=True,
    )
    return Indeterminate(
        label=name,
        attempts=attempts,
        elapsed=elapsed,
        cause=cause,
        transient_failures=1,
    )


async def connection_exists(
    client: AsyncAtlanClient, qualified_name: str
) -> Reading[bool]:
    """Search-based Connection probe — works around the direct-fetch ACL.

    Hits the indexsearch endpoint with an exact ``qualifiedName`` +
    ``typeName=Connection`` filter. The search ACL is permissive (anyone with
    read on the connector namespace can see results) whereas the direct
    entity-fetch endpoint enforces the Connection's ``adminUsers`` /
    ``adminRoles``. Use this when the harness's identity isn't expected to be on
    the Connection's admin list — e.g. when adminRoles is just ``$admin`` and the
    OAuth-client service account isn't.

    Args:
        client: An open client from :func:`atlas_client`.
        qualified_name: The Connection's exact qualified name.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying
        whether at least one Connection matched, or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        search could not be read. "Not visible yet" and "could not look" were
        both ``False`` before this split; they are different answers now.
    """
    from pyatlan.model.assets import Asset  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    label = f"Atlas Connection {qualified_name}"
    started = time.monotonic()
    try:
        request = (
            FluentSearch()
            .where(FluentSearch.active_assets())
            .where(Asset.QUALIFIED_NAME.eq(qualified_name))
            .where(Asset.TYPE_NAME.eq("Connection"))
        ).to_request()
        request.dsl.size = 0
        found = int((await client.asset.search(request)).count) > 0
    except Exception as error:
        return _unreadable(label, started, error)
    name, attempts, elapsed = _one_shot(label, started)
    return Settled(label=name, attempts=attempts, elapsed=elapsed, value=found)


class _AtlasReadFailed(RuntimeError):
    """A search that could not be read, raised so ``poll_until`` counts it.

    Not a typed leaf: it never escapes this module. ``poll_until``'s classifier
    absorbs it into the transient streak and the streak's exhaustion is what
    becomes the caller-visible
    :class:`~application_sdk.testing.harness.outcome.Indeterminate`, carrying the
    original pyatlan error as its ``__cause__``.
    """

    def __init__(self, qualified_name: str) -> None:
        super().__init__(f"Atlas search for {qualified_name} could not be read")


async def poll_for_connection(
    client: AsyncAtlanClient,
    qualified_name: str,
    *,
    budget: Budget,
) -> Outcome[bool]:
    """Poll Atlas until the Connection appears, or the budget or grace says stop.

    Expressed over :func:`~application_sdk.testing.harness.waiting.poll_until`
    rather than re-hand-rolled: the two guards this needs — "give up early when
    nothing has appeared" and "stop at the ceiling" — are exactly the start-grace
    latch and the deadline that primitive owns.

    The empty-search cap this replaced was ``max_not_found_attempts = 10``
    consecutive misses. Every probe that reached that check was an empty search
    (a hit returned immediately), so the cap fired on attempt 10 — at 9 x 30s
    elapsed. It is therefore
    :attr:`~application_sdk.testing.harness.budgets.Budget.start_grace`, at 270s
    in :data:`~application_sdk.testing.harness.budgets.CONNECTOR_CI`, and the
    verdict it produces is
    :class:`~application_sdk.testing.harness.outcome.NeverStarted` — which is
    what "the connection never appeared" says.

    The wide default ceiling (25 min) is because publish runs after the AE DAG
    completes and can take a while to flush large connections.

    Args:
        client: An open client from :func:`atlas_client`, held for the whole
            poll. One connection pool and one TLS handshake for the window,
            where the five ``asyncio.run`` bridges paid both per iteration.
        qualified_name: The Connection's exact qualified name.
        budget: The poll's allowance —
            :data:`~application_sdk.testing.harness.budgets.Wait.ATLAS_CONNECTION`
            in the shipped profile.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` ``True`` once
        the Connection is searchable;
        :class:`~application_sdk.testing.harness.outcome.NeverStarted` when the
        grace window closed on nothing but empty searches;
        :class:`~application_sdk.testing.harness.outcome.Expired` at the
        ceiling; or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when
        Atlas itself could not be read.
    """

    async def _probe() -> bool:
        reading = await connection_exists(client, qualified_name)
        if isinstance(reading, Settled):
            # conformance: ignore[L006] one line per poll of a 30s-interval loop, not a hot loop; the per-probe result is the primary diagnostic when an E2E run fails to converge
            logger.info(
                "Atlas Connection probe qn=%s exists=%s", qualified_name, reading.value
            )
            return reading.value
        # An unreadable search is a probe failure, not a "no". Raising hands the
        # streak accounting to ``poll_until``'s transient budget, which is where
        # "how many blips does this backend get" belongs — and keeps an Atlas
        # outage out of the empty-search streak that means "publish never
        # landed", which is the whole C4 miscue.
        raise _AtlasReadFailed(qualified_name) from reading.cause

    outcome = await poll_until(
        _probe,
        settled=lambda found: found,
        started=lambda found: found,
        # Only the unreadable-search signal is absorbed. A bug in the probe
        # wiring raises the same exception every attempt, so waiting out a
        # 25-minute budget on it would only delay the failure.
        transient=lambda error: (
            timedelta(0) if isinstance(error, _AtlasReadFailed) else None
        ),
        budget=budget,
        label=f"Atlas Connection {qualified_name}",
    )
    if isinstance(outcome, NeverStarted):
        logger.error(
            "Atlas Connection probe found nothing across the %.0fs grace window "
            "(%d probe(s)) — stopping early. The Connection never materialised "
            "in Atlas: publish likely reported success but the entities did not "
            "reach the asset server. Check publish metrics vs the storage bucket "
            "the worker wrote to and the one publish reads from.",
            outcome.grace.total_seconds(),
            outcome.attempts,
        )
    return outcome


async def count_assets(
    client: AsyncAtlanClient,
    connection_qualified_name: str,
    type_names: Sequence[str] = DEFAULT_TYPE_NAMES,
) -> Reading[Mapping[str, int]]:
    """Count active assets of each named type under a connection.

    Counts ACTIVE assets only: the raw index-search API returns archived
    (``__state=DELETED``) assets too, which silently inflates counts after any
    re-crawl that archives — the evolution scenario's "dropped table must leave
    the active count at baseline" assertion is meaningless without this filter
    (seen on a prior connector e2e run).

    All per-type searches share the one client and fire concurrently, which is
    the difference between ~2.7s and well under a second for the default five
    types once the handshake is paid.

    Args:
        client: An open client from :func:`atlas_client`.
        connection_qualified_name: Connection prefix to count under.
        type_names: Atlan type names to count. Empty counts nothing and returns
            an empty mapping — not the total.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying type
        name -> count, or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        search could not be read. A type with no assets is a ``0`` in a settled
        result; it is never the same value as an unreadable search.
    """
    return await _counts(
        client,
        connection_qualified_name,
        type_names,
        has_lineage_only=False,
        label=f"asset counts under {connection_qualified_name}",
    )


async def count_lineage(
    client: AsyncAtlanClient,
    connection_qualified_name: str,
    type_names: Sequence[str] = DEFAULT_TYPE_NAMES,
) -> Reading[Mapping[str, int]]:
    """Count assets of each named type that have lineage attached.

    Matches the "Lineage coverage" card in the Atlan workflow-center UI — counts
    entity assets whose ``__hasLineage`` is true under the Connection prefix.
    That's "how many of my assets did QI + lineage-app actually wire up", not
    "how many Process/ColumnProcess edges exist". The two signals are correlated
    but the asset-coverage view is what the product surfaces to reviewers, so the
    PR comment renders it verbatim.

    Args:
        client: An open client from :func:`atlas_client`.
        connection_qualified_name: Connection prefix to count under.
        type_names: Atlan type names to count.

    Returns:
        As :func:`count_assets`, including zeros so missing coverage at a level
        (e.g. no lineage on Schemas) is visible rather than hidden.
    """
    return await _counts(
        client,
        connection_qualified_name,
        type_names,
        has_lineage_only=True,
        label=f"lineage coverage under {connection_qualified_name}",
    )


async def count_total_assets(
    client: AsyncAtlanClient, connection_qualified_name: str
) -> Reading[int]:
    """Count every descendant asset under the connection prefix, ALL types.

    Unlike :func:`count_assets` (which requires explicit ``type_names``), this
    counts every asset under the connection's QN prefix regardless of type. It
    is the signal the non-empty backstop needs to protect connectors that
    declare no per-type expectations — the ones most likely to silently regress
    to a zero-asset run, and therefore the ones where "0" and "could not read"
    must not be the same number.

    Args:
        client: An open client from :func:`atlas_client`.
        connection_qualified_name: Connection prefix to count under.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying the
        count, or :class:`~application_sdk.testing.harness.outcome.Indeterminate`
        when the search could not be read.
    """
    from pyatlan.model.assets import Asset  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    label = f"total asset count under {connection_qualified_name}"
    started = time.monotonic()
    prefix = f"{connection_qualified_name}/"
    try:
        request = (
            FluentSearch()
            .where(FluentSearch.active_assets())
            .where(Asset.QUALIFIED_NAME.startswith(prefix))
            .to_request()
        )
        request.dsl.size = 0  # cheap response: only .count matters
        total = int((await client.asset.search(request)).count)
    except Exception as error:
        return _unreadable(label, started, error)
    name, attempts, elapsed = _one_shot(label, started)
    return Settled(label=name, attempts=attempts, elapsed=elapsed, value=total)


async def sample_qualified_names(
    client: AsyncAtlanClient,
    connection_qualified_name: str,
    type_names: Sequence[str],
    *,
    per_type: int = 3,
) -> Reading[Mapping[str, Sequence[str]]]:
    """Sample up to *per_type* qualified names per type under the connection.

    Backs the location/hierarchy assertion: the harness checks the *shape*
    (nesting depth) of a few landed assets per type, not just their counts.

    ``connectionQualifiedName`` is the canonical "which connection owns this
    asset" field the Atlan UI filters on, and is required to be populated on
    every asset — so the query matches on it directly, not just the QN path
    prefix, to sample the assets exactly as the product surfaces them.

    Args:
        client: An open client from :func:`atlas_client`.
        connection_qualified_name: Connection prefix to sample under.
        type_names: Atlan type names to sample.
        per_type: How many names to sample per type.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying type
        name -> sampled qualified names, or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        search could not be read. An empty list is a type that landed nothing;
        before this split it was also what a failed search returned, which made
        the location check pass silently on an Atlas outage.
    """
    from pyatlan.model.assets import Asset  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    label = f"qualified-name samples under {connection_qualified_name}"
    started = time.monotonic()
    if not type_names:
        name, attempts, elapsed = _one_shot(label, started)
        return Settled(label=name, attempts=attempts, elapsed=elapsed, value={})
    prefix = f"{connection_qualified_name}/"
    connection_qn = connection_qualified_name

    async def _sample_one(type_name: str) -> list[str]:
        request = (
            FluentSearch()
            .where(FluentSearch.active_assets())
            .where(Asset.QUALIFIED_NAME.startswith(prefix))
            .where(Asset.CONNECTION_QUALIFIED_NAME.eq(connection_qn))
            .where(Asset.TYPE_NAME.eq(type_name))
            .include_on_results(Asset.QUALIFIED_NAME)
            .include_on_results(Asset.CONNECTION_QUALIFIED_NAME)
        ).to_request()
        request.dsl.size = per_type
        results = await client.asset.search(request)
        page = results.current_page() or []
        # Asset.qualified_name is str | None; the `if qn` narrows it to str so
        # the return stays list[str], and the len cap enforces per_type without
        # a trailing slice.
        qns: list[str] = []
        for asset in page:
            qn = asset.qualified_name
            if qn:
                qns.append(qn)
            if len(qns) >= per_type:
                break
        return qns

    try:
        sampled = await asyncio.gather(*(_sample_one(tn) for tn in type_names))
    except Exception as error:
        return _unreadable(label, started, error)
    name, attempts, elapsed = _one_shot(label, started)
    return Settled(
        label=name,
        attempts=attempts,
        elapsed=elapsed,
        value=dict(zip(type_names, sampled, strict=True)),
    )


async def _counts(
    client: AsyncAtlanClient,
    connection_qualified_name: str,
    type_names: Sequence[str],
    *,
    has_lineage_only: bool,
    label: str,
) -> Reading[Mapping[str, int]]:
    """Parallel per-type ``count`` searches, as one settled mapping or none.

    One failed type makes the whole mapping
    :class:`~application_sdk.testing.harness.outcome.Indeterminate` rather than
    zeroing that one entry. Partially-read counts are the fail-open bug in
    miniature: a mapping with four real numbers and one silent zero is graded as
    a real reading, and the zero is the one an expectation trips on.
    """
    from pyatlan.model.assets import Asset  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    started = time.monotonic()
    if not type_names:
        name, attempts, elapsed = _one_shot(label, started)
        return Settled(label=name, attempts=attempts, elapsed=elapsed, value={})
    prefix = f"{connection_qualified_name}/"

    async def _count_one(type_name: str) -> int:
        builder = (
            FluentSearch()
            .where(FluentSearch.active_assets())
            .where(Asset.QUALIFIED_NAME.startswith(prefix))
            .where(Asset.TYPE_NAME.eq(type_name))
        )
        if has_lineage_only:
            builder = builder.where(Asset.HAS_LINEAGE.eq(True))
        request = builder.to_request()
        request.dsl.size = 0  # cheap response: we only want .count
        return int((await client.asset.search(request)).count)

    try:
        counts = await asyncio.gather(*(_count_one(tn) for tn in type_names))
    except Exception as error:
        return _unreadable(label, started, error)
    name, attempts, elapsed = _one_shot(label, started)
    return Settled(
        label=name,
        attempts=attempts,
        elapsed=elapsed,
        value=dict(zip(type_names, counts, strict=True)),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminIdentity:
    """Who may administer a Connection this run creates.

    Both halves are the *fallback* — what the harness resolves when a suite
    pinned no explicit ACL of its own. Neither is optional in practice, and they
    fail differently, which is why they are one value rather than two reads a
    caller has to remember to pair:

    * without a role, Atlas refuses the Connection outright
      (``ATLAS-400-00-114``: at least one admin list must be non-empty);
    * without the *creating* identity in ``adminUsers``, the connection is
      created and then cannot be purged — the ``$admin`` role alone is not
      enough when the service account does not hold it, so teardown 403s
      (``ATLAS-403-00-001``) and orphans the connection and everything under it
      on a shared tenant, while the leg still passes because a teardown failure
      is only a warning.

    Attributes:
        roles: Role GUIDs, empty when the role could not be resolved.
        users: Usernames, empty when the current user could not be read.
    """

    roles: tuple[str, ...] = ()
    users: tuple[str, ...] = ()


async def admin_identity(
    client: AsyncAtlanClient, *, role_name: str = ADMIN_ROLE_NAME
) -> Reading[AdminIdentity]:
    """Resolve the admin ACL fallback for a harness-created Connection.

    **One reader, two policies.** ``BaseE2ETest.setup_method`` degraded to an
    empty ACL on any failure and let Atlas reject the Connection later, while
    ``SQLAppE2ETest._resolve_admin_role_guid`` raised
    ``AdminRoleNotResolvedError`` on an absent role — two implementations of one
    lookup that disagreed about what an unresolvable ``$admin`` means. The
    disagreement is a *policy*, so it stays with the callers: this returns the
    reading, and each decides whether an empty answer is fatal.

    Args:
        client: An open client from :func:`atlas_client`.
        role_name: Role to resolve. Defaults to :data:`ADMIN_ROLE_NAME`.

    Returns:
        :class:`~application_sdk.testing.harness.outcome.Settled` carrying the
        identity — whose fields are *empty* when the tenant simply has no such
        role or no resolvable current user — or
        :class:`~application_sdk.testing.harness.outcome.Indeterminate` when the
        lookup could not be performed at all. The two are different answers: a
        tenant without a ``$admin`` role is a finding about the tenant, and an
        unreachable role cache is a finding about the network.
    """
    label = f"admin identity ({role_name}) on the tenant"
    started = time.monotonic()
    try:
        guid = await client.role_cache.get_id_for_name(role_name)
        current = await client.user.get_current()
    except Exception as error:
        return _unreadable(label, started, error)
    username = getattr(current, "username", "") or ""
    if not guid:
        logger.warning(
            "the %s role is not present on this tenant, so a Connection created "
            "with no explicit admin_roles will be rejected (ATLAS-400-00-114) — "
            "set connection_admin_roles on the suite",
            role_name,
        )
    name, attempts, elapsed = _one_shot(label, started)
    return Settled(
        label=name,
        attempts=attempts,
        elapsed=elapsed,
        value=AdminIdentity(
            roles=(guid,) if guid else (),
            users=(username,) if username else (),
        ),
    )


async def create_connection(
    client: AsyncAtlanClient,
    *,
    qualified_name: str,
    display_name: str,
    connector_type: str,
    admin_users: Sequence[str] = (),
    admin_groups: Sequence[str] = (),
    admin_roles: Sequence[str] = (),
) -> str:
    """Create a Connection at an exact qualified name, and return that name.

    **The qualified name is an input, not an output**, and that is the one
    behavioural difference from the ``Connection.creator`` call this replaces.
    ``creator`` derives the name itself as ``default/<type>/<epoch>`` — one
    second of resolution — and the lifted code then *adopted* whatever came
    back, discarding the run's own minted name. Two legs of one e2e matrix
    starting in the same second therefore shared a connection, and the first to
    finish purged the other's assets. Taking the name from
    :meth:`~application_sdk.testing.harness.identity.Minter.connection_identity`
    is what makes the run's teardown target its own connection and nobody
    else's, and what lets a test predict the name at all.

    Args:
        client: An open client from :func:`atlas_client`.
        qualified_name: Exact qualified name to create at, from the minter.
        display_name: Human-facing name on the same connection.
        connector_type: Atlan connector type value — the catalog segment, e.g.
            ``"api"`` or ``"postgres"``.
        admin_users: Usernames on the connection's admin ACL.
        admin_groups: Group aliases on the admin ACL.
        admin_roles: Role GUIDs on the admin ACL.

    Returns:
        *qualified_name*, unchanged. Returned rather than assumed so the call
        site reads as an adoption and cannot drift back into deriving one.

    Raises:
        UnknownConnectorTypeError: *connector_type* is not a pyatlan
            ``AtlanConnectorType``.
        Exception: Whatever pyatlan raises if the save is rejected — an empty
            admin ACL (``ATLAS-400-00-114``), an unknown role GUID, a
            network fault. Not collapsed into a verdict: unlike every read in
            this module, a failed *write* leaves the caller with no connection
            at all, and there is no degraded mode to report.
    """
    from pyatlan.model.assets import Connection  # noqa: PLC0415
    from pyatlan.model.enums import AtlanConnectorType  # noqa: PLC0415

    try:
        resolved = AtlanConnectorType(connector_type)
    except ValueError as error:
        raise UnknownConnectorTypeError(
            message=(
                f"cannot create a Connection because {connector_type!r} is not a "
                "pyatlan AtlanConnectorType. Pass the Atlan catalog type segment "
                "(e.g. 'api' for the OpenAPI connector, whose short name is "
                "'openapi')"
            ),
            value_summary=connector_type,
        ) from error

    # The three validations ``Connection.creator`` performs, on the async caches.
    # Kept because they are what turns a typo in an ACL into an error naming the
    # bad value, rather than an Atlas rejection naming the whole request.
    await client.user_cache.validate_names(names=list(admin_users))
    await client.role_cache.validate_idstrs(idstrs=list(admin_roles))
    await client.group_cache.validate_aliases(aliases=list(admin_groups))

    connection = Connection(
        attributes=Connection.Attributes(
            name=display_name,
            qualified_name=qualified_name,
            connector_name=resolved.value,
            category=resolved.category.value,
            admin_users=set(admin_users),
            admin_groups=set(admin_groups),
            admin_roles=set(admin_roles),
        )
    )
    # The negative placeholder ``Connection.creator`` gets from ``@init_guid``:
    # Atlas assigns the real GUID, and a create without one is rejected.
    connection.guid = str(-secrets.randbelow(10_000_000_000_000_000) - 1)
    await client.asset.save(connection)
    logger.info("harness: created connection %s", qualified_name)
    return qualified_name
