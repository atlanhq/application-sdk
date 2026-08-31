"""Unit tests for the purge mechanics (child G, FND-243).

**A differential, where one is available.** This lifts
``BaseE2ETest._purge_child_assets`` and ``_purge_connection``, so the properties
worth pinning are the ones those two encoded — and every one of them is there
because it went wrong once:

* **Batching.** ``purge_by_guid`` puts one ``guid=`` query parameter per asset
  into a single DELETE and ``httpx`` refuses a URL past ``MAX_URL_LENGTH``, so
  an unbatched purge of a normal crawl deletes *nothing*. The batch-boundary
  test is therefore a correctness test, not a performance one.
* **Read everything, then delete.** Atlas' default pagination is offset-based,
  so deleting between pages shifts the window and skips assets — silently, with
  a clean-looking report.
* **Two independent guards.** One shared guard is what let a failing child batch
  orphan the connection too: the raise jumped the connection purge, and the
  warning is post-verdict, so the leg went green while leaking everything.
* **Nothing raises.** Teardown runs after the assertions decided the verdict.

The one place this deliberately does *more* than the original is
:attr:`PurgeReport.orphaned`: the lifted code counted failed batches, and a
count tells an operator only that they have a problem. The names are what they
can act on.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.harness.teardown import (
    PURGE_BATCH_SIZE,
    PurgeReport,
    purge_connection,
)
from tests.unit.testing._atlas_fakes import FakeAsset, FakeAtlasClient, FakeSearchResult

_QN = "default/postgres/1700000000042"


def _is_connection_search(request: Any) -> bool:
    """Which of the two searches this is, read off the request the code built.

    Off the real ``FluentSearch`` output rather than off call ordering, so a
    reordering of the two phases cannot make a test pass by coincidence.
    """
    return "Connection" in str(request.dsl.query)


def _client(
    *,
    children: list[FakeAsset] | None = None,
    connection: list[FakeAsset] | None = None,
    on_purge: Any = None,
) -> FakeAtlasClient:
    """A client answering the listing search and the connection search."""
    resolved_connection = (
        [FakeAsset(_QN, "guid-connection")] if connection is None else connection
    )

    def _behavior(request: Any) -> FakeSearchResult:
        if _is_connection_search(request):
            return FakeSearchResult(resolved_connection)
        return FakeSearchResult(children or [])

    return FakeAtlasClient(_behavior, on_purge=on_purge)


def _children(count: int) -> list[FakeAsset]:
    return [FakeAsset(f"{_QN}/t{index}", f"guid-{index}") for index in range(count)]


# ---------------------------------------------------------------------------
# Batching, which is a correctness bound
# ---------------------------------------------------------------------------


async def test_a_large_purge_is_chunked_under_the_url_ceiling():
    """The property an unbatched purge fails: no single DELETE names them all.

    Asserted as "every batch is at most PURGE_BATCH_SIZE" rather than as an
    exact call count, so raising the constant stays a one-line change and
    lowering it below the ceiling stays legal — what must never happen is one
    call carrying 120 GUIDs, which is what the ceiling rejects.
    """
    client = _client(children=_children(120))

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    child_batches = [call for call in client.asset.purged if isinstance(call, list)]
    assert child_batches
    assert all(len(batch) <= PURGE_BATCH_SIZE for batch in child_batches)
    assert sum(len(batch) for batch in child_batches) == 120
    assert report.purged == 121  # the 120 children plus the connection
    assert report.complete


async def test_every_listed_asset_is_purged_exactly_once():
    """No GUID is dropped between batches and none is sent twice."""
    client = _client(children=_children(PURGE_BATCH_SIZE * 2 + 7))

    await purge_connection(client, _QN)  # type: ignore[arg-type]

    sent = [
        guid for call in client.asset.purged if isinstance(call, list) for guid in call
    ]
    assert sent == [f"guid-{index}" for index in range(PURGE_BATCH_SIZE * 2 + 7)]


async def test_the_whole_listing_is_read_before_anything_is_deleted():
    """Offset-based pagination shifts under a delete, so the order is the fix.

    Pinned by watching the interleaving: if a single search preceded each
    delete, this run would show ``search, purge, search, purge`` and a real
    Atlas would skip a page per batch while reporting a clean teardown.
    """
    order: list[str] = []

    def _record_purge(_guid: Any) -> None:
        order.append("purge")

    client = _client(children=_children(75), on_purge=_record_purge)
    original_search = client.asset.search

    async def _recording_search(request: Any) -> Any:
        order.append("search")
        return await original_search(request)

    client.asset.search = _recording_search  # type: ignore[method-assign]

    await purge_connection(client, _QN)  # type: ignore[arg-type]

    # 75 children at a batch of 50: one listing search, two child DELETEs, then
    # the connection's own search and DELETE. Pinned exactly rather than as a
    # property, because the failure this guards against is a *reordering* and an
    # exact sequence is the only assertion a reordering cannot satisfy.
    assert order == ["search", "purge", "purge", "search", "purge"]


# ---------------------------------------------------------------------------
# Nothing raises, and one failure does not become two
# ---------------------------------------------------------------------------


async def test_a_failing_batch_orphans_only_that_batch():
    """The rest still goes, and the report names what did not."""
    calls = {"n": 0}

    def _fail_second(guid: Any) -> None:
        if isinstance(guid, list):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("atlas said no")

    client = _client(children=_children(PURGE_BATCH_SIZE * 3), on_purge=_fail_second)

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    assert report.purged == PURGE_BATCH_SIZE * 2 + 1  # two batches plus the connection
    assert len(report.orphaned) == PURGE_BATCH_SIZE
    # Qualified names, not GUIDs: the list has to be actionable by hand.
    assert all(name.startswith(f"{_QN}/") for name in report.orphaned)
    assert len(report.errors) == 1
    assert not report.complete


async def test_a_failing_child_purge_does_not_orphan_the_connection():
    """The two phases are independently guarded, which is the FND fix.

    Sharing one guard is what let a single batch failure jump past the
    connection purge — and because the teardown warning is post-verdict, the leg
    still went green while leaking the connection *and* everything under it.
    """

    def _fail_children(guid: Any) -> None:
        if isinstance(guid, list):
            raise RuntimeError("atlas said no")

    client = _client(children=_children(10), on_purge=_fail_children)

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    assert "guid-connection" in client.asset.purged
    assert report.purged == 1
    assert _QN not in report.orphaned


async def test_an_unreadable_listing_still_purges_the_connection():
    """Nothing is known about the children, so nothing below can be deleted.

    The connection purge needs no listing and still runs — a connection left
    behind is exactly the half-set-up leftover that greens a later run which
    should have failed.
    """

    def _behavior(request: Any) -> FakeSearchResult:
        if _is_connection_search(request):
            return FakeSearchResult([FakeAsset(_QN, "guid-connection")])
        raise RuntimeError("elasticsearch is unhappy")

    client = FakeAtlasClient(_behavior)

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    assert client.asset.purged == ["guid-connection"]
    assert report.purged == 1
    assert report.errors and not report.complete
    # Nothing is *named* as orphaned: the listing established nothing, so the
    # report must not claim to know what was left behind.
    assert report.orphaned == ()


@pytest.mark.parametrize(
    "failing",
    ["listing", "children", "connection"],
    ids=["unreadable listing", "failed batch", "failed connection purge"],
)
async def test_no_failure_escapes_as_an_exception(failing: str):
    """Teardown runs after the verdict; raising here replaces a real failure."""

    def _behavior(request: Any) -> FakeSearchResult:
        if failing == "listing" and not _is_connection_search(request):
            raise RuntimeError("boom")
        if _is_connection_search(request):
            return FakeSearchResult([FakeAsset(_QN, "guid-connection")])
        return FakeSearchResult(_children(3))

    def _on_purge(guid: Any) -> None:
        if failing == "children" and isinstance(guid, list):
            raise RuntimeError("boom")
        if failing == "connection" and not isinstance(guid, list):
            raise RuntimeError("boom")

    report = await purge_connection(  # type: ignore[arg-type]
        FakeAtlasClient(_behavior, on_purge=_on_purge), _QN
    )

    assert isinstance(report, PurgeReport)
    assert report.errors and not report.complete


# ---------------------------------------------------------------------------
# The nothing-to-do cases, which must not look like failures
# ---------------------------------------------------------------------------


async def test_a_run_that_created_nothing_reports_a_complete_purge():
    """ "Nothing to delete" and "everything deleted" are one outcome for a tenant.

    Reporting the first as incomplete would send an operator to a tenant to
    clean up assets that were never created — most often after a run that failed
    during seeding, which is when a false alarm is least welcome.
    """
    client = _client(children=[], connection=[])

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    assert client.asset.purged == []
    assert report == PurgeReport()
    assert report.complete


async def test_an_asset_with_no_guid_is_reported_orphaned_not_dropped():
    """A hit with no GUID cannot be deleted, so it must be *named*.

    The first revision of this test asserted the opposite — that the hit is
    simply skipped — which pinned a real bug rather than a behaviour. Dropping
    it takes it out of the report as well as out of the DELETE, so the
    connection purge then succeeds and `complete` answers True over an asset
    still sitting on a shared tenant. Every hit the search returned has to land
    somewhere on the report; `purge_by_guid` being the only delete available is
    a reason it is orphaned, not a reason it is invisible.
    """
    client = _client(
        children=[FakeAsset(f"{_QN}/t0", ""), FakeAsset(f"{_QN}/t1", "guid-1")]
    )

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    assert client.asset.purged[0] == ["guid-1"]
    assert report.purged == 2  # one child plus the connection
    assert report.orphaned == (f"{_QN}/t0",)
    assert report.errors and not report.complete


async def test_every_listed_hit_lands_somewhere_on_the_report():
    """The invariant the accounting rests on: purged + orphaned covers the search.

    Stated as a property over a mixed listing rather than as three separate
    cases, because the failure it guards against is a hit falling through a gap
    between them — which no single-case test sees.
    """
    listed = [
        FakeAsset(f"{_QN}/ok0", "guid-0"),
        FakeAsset(f"{_QN}/noguid", ""),
        FakeAsset(f"{_QN}/ok1", "guid-1"),
    ]
    client = _client(children=listed)

    report = await purge_connection(client, _QN)  # type: ignore[arg-type]

    # -1 for the connection itself, which was not part of the child listing.
    assert (report.purged - 1) + len(report.orphaned) == len(listed)
    assert report.orphaned == (f"{_QN}/noguid",)
    assert not report.complete
