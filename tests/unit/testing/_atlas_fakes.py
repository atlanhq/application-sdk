"""A stand-in for pyatlan's ``AsyncAtlanClient``, for the harness's Atlas reads.

Only the network call is faked. ``FluentSearch`` still builds the real request,
so a change to a filter, a page size or an ``include_on_results`` is still
exercised — what the fake replaces is ``asset.search``, and nothing above it.

Shared by the harness's own Atlas tests, by the tests of the sync
``AEWorkflowClient`` adapter over them, and by the teardown purge (child G) —
so every consumer drives the same seam. The purge is why the result grew
``__aiter__`` and why the asset API grew ``purge_by_guid``: a purge reads with
the same ``asset.search`` the counts do and then writes through the one verb
next to it, and a sibling double would let the two disagree about what a page
looks like.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeAsset:
    """One search hit, carrying the attributes the readers look at.

    ``guid`` defaults to one derived from the qualified name rather than to
    ``None``: every existing caller constructs these positionally by name, and a
    default of ``None`` would make them invisible to the purge — which skips a
    hit with no GUID — turning a real regression into a passing test.
    """

    def __init__(self, qualified_name: str, guid: str | None = None) -> None:
        self.qualified_name = qualified_name
        self.guid = f"guid-{qualified_name}" if guid is None else guid


class FakeSearchResult:
    """A search response: a ``count`` for the counting reads, a page for samples.

    Iterable both ways. ``current_page`` is what the sampling reads call;
    ``async for`` is what the purge's listing uses, and pyatlan's own
    ``AsyncSearchResults.__aiter__`` pages until exhaustion — one page here,
    because a fake that lied about pagination would hide the very bug the
    read-everything-before-deleting order exists to prevent.
    """

    def __init__(
        self, assets: list[FakeAsset] | None = None, *, count: int | None = None
    ) -> None:
        self._assets = assets or []
        self.count = len(self._assets) if count is None else count

    def current_page(self) -> list[FakeAsset]:
        return self._assets

    async def __aiter__(self):
        for asset in self._assets:
            yield asset


class FakeAssetApi:
    """``client.asset``, recording every request so a test can count the calls.

    Attributes:
        requests: Every search request, in order.
        purged: Every ``purge_by_guid`` argument, in order — a list of GUIDs or
            a single GUID, recorded exactly as it was passed so a test can pin
            the batch boundaries rather than only the total.
    """

    def __init__(
        self,
        behavior: Callable[[Any], Any],
        *,
        on_purge: Callable[[Any], None] | None = None,
    ) -> None:
        self._behavior = behavior
        self._on_purge = on_purge
        self.requests: list[Any] = []
        self.purged: list[Any] = []

    async def search(self, request: Any) -> Any:
        self.requests.append(request)
        return self._behavior(request)

    async def purge_by_guid(self, guid: Any) -> None:
        self.purged.append(guid)
        if self._on_purge is not None:
            self._on_purge(guid)


class FakeAtlasClient:
    """What :func:`~application_sdk.testing.harness.atlas.atlas_client` yields."""

    def __init__(
        self,
        behavior: Callable[[Any], Any],
        *,
        on_purge: Callable[[Any], None] | None = None,
    ) -> None:
        self.asset = FakeAssetApi(behavior, on_purge=on_purge)

    @property
    def searches(self) -> int:
        """How many searches this one client served — the reuse assertion."""
        return len(self.asset.requests)


class FakeAtlasClientCM:
    """An ``async with`` wrapper, counting how many times it was entered.

    :attr:`opens` is the number that made the five ``asyncio.run`` bridges worth
    removing: it used to be one per probe of a 1,500-second poll.
    """

    def __init__(self, client: FakeAtlasClient) -> None:
        self._client = client
        self.opens = 0

    async def __aenter__(self) -> FakeAtlasClient:
        self.opens += 1
        return self._client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def fake_atlas(behavior: Callable[[Any], Any]) -> FakeAtlasClientCM:
    """Build a one-client context manager whose searches run *behavior*."""
    return FakeAtlasClientCM(FakeAtlasClient(behavior))
