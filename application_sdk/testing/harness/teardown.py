"""Purging what a harness run created.

Lifted from ``BaseE2ETest.teardown_method``, whose purge path is the part of the
harness with the least test coverage and the most consequence: assets a failed
run leaves behind accumulate in a shared tenant, and a leftover half-set-up
connection is exactly what greens a later run that should have failed.

Three mechanics have to survive the lift, and none of them is obvious.

**Batching is a hard requirement, not a tuning knob.** ``pyatlan``'s
``purge_by_guid`` puts one ``guid=`` query parameter per asset into a single
DELETE, and ``httpx`` refuses to build a URL whose query exceeds its
``MAX_URL_LENGTH``. At roughly 42 bytes per GUID that is a ceiling near 1,550
assets, raised client-side — so an unbatched purge of a normal crawl's output
deletes *nothing*. The batch stays well under the ceiling for a second reason:
purge is expensive server-side, and a smaller batch means a failing chunk
orphans less.

**The whole listing is read before anything is deleted.** Atlas' default search
pagination is offset-based (``from``/``size``), so purging between pages shifts
the result window and silently skips assets — the run reports a clean teardown
having deleted every other page. A list of GUID/qualified-name pairs is cheap
even at six figures, and it is the only ordering under which the count in the
report means what it says.

**A failed purge is reported, never raised.** Teardown runs after the assertions
have already decided the verdict. Raising here replaces a real failure with a
cleanup error and loses the diagnosis. Every path in this module returns a
:class:`PurgeReport` — there is no exception a caller has to catch to stay
correct, which is a stronger guarantee than "we remembered to wrap the call".

**Two independently-guarded phases**, and that is load-bearing rather than
stylistic. Sharing one guard is what let a single child-purge failure orphan the
connection as well: the raise jumped past the connection purge entirely, and
because the teardown warning is post-verdict the leg still went green while
leaking everything under it.

What deliberately does *not* live here is the policy: which connection to purge,
and whether to purge at all. This module takes a qualified name a run minted for
itself. Pointing it at a long-lived shared connection would delete assets that
are not the run's to delete, and no guard here can tell the two apart — which is
why :mod:`application_sdk.testing.harness.identity` mints the ephemeral name in
the first place, and why a test can predict it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from application_sdk.errors.base import sanitize_cause_repr
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only; pyatlan is a lazy import
    from pyatlan.client.aio.client import AsyncAtlanClient

logger = get_logger(__name__)

__all__ = ["PURGE_BATCH_SIZE", "PurgeReport", "purge_connection"]

#: Assets per DELETE. See the module docstring for why this is a correctness
#: bound rather than a performance choice.
PURGE_BATCH_SIZE = 50

#: Assets per search page while listing what to purge. Matches the ``dsl.size =
#: 200`` the lifted implementation used. Unrelated to :data:`PURGE_BATCH_SIZE`:
#: this one bounds a GET's response, that one bounds a DELETE's URL.
_LISTING_PAGE_SIZE = 200


@dataclass(frozen=True, slots=True, kw_only=True)
class PurgeReport:
    """What a purge managed to delete, and what it did not.

    Attributes:
        purged: Count of assets successfully deleted, the connection included
            when it was.
        orphaned: Qualified names of assets the purge could not delete. Named
            individually rather than counted: an operator cleaning up by hand
            needs the list, and a count tells them only that they have a problem.
        errors: One line per failed step, in order. Already secret-redacted —
            a pyatlan error can quote the request URL, and a report that ships
            with an evidence bundle is not the place to find out.
    """

    purged: int = 0
    orphaned: Sequence[str] = field(default_factory=tuple)
    errors: Sequence[str] = field(default_factory=tuple)

    @property
    def complete(self) -> bool:
        """Did the purge finish with nothing left behind and nothing failing?

        Returns:
            ``True`` when no asset was orphaned and no step errored. A run that
            created nothing purges nothing and is still complete — "there was
            nothing to delete" and "everything was deleted" are the same
            outcome for the tenant, and neither needs a person.
        """
        return not self.orphaned and not self.errors


async def purge_connection(
    client: AsyncAtlanClient, connection_qualified_name: str
) -> PurgeReport:
    """Delete every asset under *connection_qualified_name*, then the connection.

    Args:
        client: An open client from
            :func:`~application_sdk.testing.harness.atlas.atlas_client`, held for
            the whole purge. Taken as a parameter for the reason every Atlas read
            in this harness takes one: a purge of a large crawl is hundreds of
            round trips, and a function that opened its own would pay a TLS
            handshake per call.
        connection_qualified_name: The ephemeral connection this run created.
            Must be a name the run minted itself — never a long-lived shared
            connection, whose assets are not this run's to delete.

    Returns:
        A :class:`PurgeReport`. Failures are reported here, not raised: see the
        module docstring. The two phases are independent, so a child purge that
        fails entirely still leaves the connection purge to run.

    Example:
        >>> async with atlas_client(url, token) as client:  # doctest: +SKIP
        ...     report = await purge_connection(client, identity.qualified_name)
        >>> if not report.complete:  # doctest: +SKIP
        ...     logger.warning("manual purge needed: %s", report.orphaned)
    """
    listing = await _list_children(client, connection_qualified_name)
    # An unreadable listing is the one failure that is not partial: nothing is
    # known about what is under the connection, so nothing below it can be
    # purged, and nothing can be *named* as left behind either — which is what
    # the error line has to say instead. The connection purge still runs: it
    # needs no listing, and a connection left behind is exactly the leftover
    # that greens a later run.
    #
    # A hit with no GUID is the other shape, and it is not the same one: the
    # listing succeeded, the asset is known by name, and it simply cannot be
    # deleted. It is folded in as orphaned so it appears on the report — a hit
    # that is neither purged nor named would leave an asset on a shared tenant
    # behind a report claiming the teardown was clean.
    purged = await _purge_children(client, connection_qualified_name, listing.assets)
    child_report = PurgeReport(
        purged=purged.purged,
        orphaned=(*listing.unusable, *purged.orphaned),
        errors=(*listing.errors, *purged.errors),
    )
    connection_report = await _purge_connection_asset(client, connection_qualified_name)
    report = PurgeReport(
        purged=child_report.purged + connection_report.purged,
        orphaned=(*child_report.orphaned, *connection_report.orphaned),
        errors=(*child_report.errors, *connection_report.errors),
    )
    if report.complete:
        logger.info(
            "harness teardown: purged %d asset(s) under %s",
            report.purged,
            connection_qualified_name,
        )
    else:
        logger.warning(
            "harness teardown: purged %d asset(s) under %s but left %d behind — "
            "manual purge may be needed. %s",
            report.purged,
            connection_qualified_name,
            len(report.orphaned),
            "; ".join(report.errors),
        )
    return report


@dataclass(frozen=True, slots=True, kw_only=True)
class _Listing:
    """What one listing pass established about a connection's descendants.

    Three fields rather than two because a hit can fail to be purgeable without
    the *listing* having failed: see :attr:`unusable`. Every hit lands in exactly
    one of them, which is what lets the caller build a report that accounts for
    everything the search returned.

    Attributes:
        assets: ``(guid, qualified_name)`` for every hit that can be deleted.
        unusable: Qualified names of hits Atlas returned with no GUID. Nothing
            can delete these — ``purge_by_guid`` is the only verb available —
            so they are orphaned from the moment they are read.
        errors: One line per problem. Populated alongside :attr:`unusable`, or
            alone when the search itself could not be read (in which case
            nothing is known and both other fields are empty — a partial listing
            would be a purge that under-reports what it left behind).
    """

    assets: Sequence[tuple[str, str]] = field(default_factory=tuple)
    unusable: Sequence[str] = field(default_factory=tuple)
    errors: Sequence[str] = field(default_factory=tuple)


async def _list_children(
    client: AsyncAtlanClient, connection_qualified_name: str
) -> _Listing:
    """Read every descendant's GUID and qualified name, in one pass.

    Both fields, not just the GUID the DELETE needs: :attr:`PurgeReport.orphaned`
    names what was left behind, and a list of GUIDs is not something an operator
    can act on without a second round of lookups against a tenant they are
    cleaning up by hand.

    Returns:
        A :class:`_Listing`. **Every hit the search returned appears in exactly
        one of its fields** — that is the invariant, and it is the one a purge
        report depends on: a hit that is neither purged nor named as orphaned is
        an asset left on a shared tenant behind a report that says the teardown
        was clean.
    """
    from pyatlan.model.assets import Asset  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    prefix = f"{connection_qualified_name}/"
    try:
        request = (
            FluentSearch()
            .where(Asset.QUALIFIED_NAME.startswith(prefix))
            .include_on_results(Asset.GUID)
            .include_on_results(Asset.QUALIFIED_NAME)
        ).to_request()
        request.dsl.size = _LISTING_PAGE_SIZE
        results = await client.asset.search(request)
        assets: list[tuple[str, str]] = []
        unusable: list[str] = []
        async for asset in results:
            if asset.guid:
                assets.append((asset.guid, asset.qualified_name or asset.guid))
            else:
                # Not skipped. `purge_by_guid` is the only delete available, so
                # a hit Atlas could not attribute a GUID to cannot be deleted —
                # but it exists, under this connection, and dropping it here
                # would take it out of the report as well as out of the DELETE.
                # The connection purge then succeeds and `complete` answers True
                # over an asset still sitting on the tenant.
                unusable.append(asset.qualified_name or "<asset with no GUID>")
    except Exception as error:
        logger.warning(
            "harness teardown: could not list assets under %s — nothing below the "
            "connection can be purged, and what is there is unknown rather than "
            "absent",
            connection_qualified_name,
            exc_info=True,
        )
        return _Listing(
            errors=(
                f"listing assets under {connection_qualified_name} failed: "
                f"{sanitize_cause_repr(error)}",
            )
        )
    errors: tuple[str, ...] = ()
    if unusable:
        errors = (
            f"{len(unusable)} listed asset(s) under {connection_qualified_name} "
            "had no GUID and could not be purged",
        )
    return _Listing(assets=tuple(assets), unusable=tuple(unusable), errors=errors)


async def _purge_children(
    client: AsyncAtlanClient,
    connection_qualified_name: str,
    assets: Sequence[tuple[str, str]],
) -> PurgeReport:
    """DELETE the listed assets in :data:`PURGE_BATCH_SIZE` chunks.

    One failing batch orphans that batch and no more. Per-batch errors are
    collected rather than logged individually: a tenant-wide outage is hundreds
    of failing batches, and one warning each buries the aggregate count that
    actually says how much leaked.
    """
    purged = 0
    orphaned: list[str] = []
    errors: list[str] = []
    for offset in range(0, len(assets), PURGE_BATCH_SIZE):
        batch = assets[offset : offset + PURGE_BATCH_SIZE]
        try:
            await client.asset.purge_by_guid([guid for guid, _ in batch])
        # conformance: ignore[E004] teardown boundary — a batch failure must never propagate and mask the test verdict; every batch is accounted for in the returned report and the aggregate is logged at WARNING by the caller
        except Exception as error:
            # DEBUG per batch, and the caller's single WARNING for the summary:
            # see the docstring above on why the aggregate is the useful line.
            logger.debug(
                "harness teardown: purge batch at offset %d failed",
                offset,
                exc_info=True,
            )
            orphaned.extend(qualified_name for _, qualified_name in batch)
            errors.append(
                f"purge of {len(batch)} asset(s) at offset {offset} under "
                f"{connection_qualified_name} failed: {sanitize_cause_repr(error)}"
            )
        else:
            purged += len(batch)
    return PurgeReport(purged=purged, orphaned=tuple(orphaned), errors=tuple(errors))


async def _purge_connection_asset(
    client: AsyncAtlanClient, connection_qualified_name: str
) -> PurgeReport:
    """Delete the connection itself, once its descendants are gone.

    A connection that is not found is not an error and not an orphan. Teardown
    is called on every path, including one where seeding failed before the
    connection was created and one where a previous teardown already removed it;
    reporting "nothing there" as a failure would make a clean run look dirty.
    """
    from pyatlan.model.assets import Asset  # noqa: PLC0415
    from pyatlan.model.fluent_search import FluentSearch  # noqa: PLC0415

    try:
        request = (
            FluentSearch()
            .where(Asset.QUALIFIED_NAME.eq(connection_qualified_name))
            .where(Asset.TYPE_NAME.eq("Connection"))
            .include_on_results(Asset.GUID)
        ).to_request()
        request.dsl.size = 1
        results = await client.asset.search(request)
        guid: str | None = None
        # Bounded to a single result via ``dsl.size = 1``, and to one purge event
        # per connection. The lifted implementation carried an ``L006`` waiver
        # saying exactly that, because its version logged from inside the loop;
        # this one does not, so the rule no longer fires and the waiver would be
        # an inert directive claiming to suppress something. The bound is the
        # part that had to survive the lift, not the suppression.
        async for asset in results:
            guid = asset.guid or guid
        if guid is None:
            logger.info(
                "harness teardown: no Connection at %s to purge",
                connection_qualified_name,
            )
            return PurgeReport()
        await client.asset.purge_by_guid(guid)
    except Exception as error:
        logger.warning(
            "harness teardown: could not purge the connection %s — manual purge "
            "may be needed",
            connection_qualified_name,
            exc_info=True,
        )
        return PurgeReport(
            orphaned=(connection_qualified_name,),
            errors=(
                f"purge of connection {connection_qualified_name} failed: "
                f"{sanitize_cause_repr(error)}",
            ),
        )
    logger.info("harness teardown: purged connection %s", connection_qualified_name)
    return PurgeReport(purged=1)
