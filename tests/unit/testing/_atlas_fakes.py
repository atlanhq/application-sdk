"""A stand-in for pyatlan's ``AsyncAtlanClient``, for the harness's Atlas reads.

Only the network call is faked. ``FluentSearch`` still builds the real request,
so a change to a filter, a page size or an ``include_on_results`` is still
exercised — what the fake replaces is ``asset.search``, and nothing above it.

Shared by the harness's own Atlas tests and by the tests of the sync
``AEWorkflowClient`` adapter over them, so both drive the same seam.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeAsset:
    """One search hit, carrying the single attribute the readers look at."""

    def __init__(self, qualified_name: str) -> None:
        self.qualified_name = qualified_name


class FakeSearchResult:
    """A search response: a ``count`` for the counting reads, a page for samples."""

    def __init__(
        self, assets: list[FakeAsset] | None = None, *, count: int | None = None
    ) -> None:
        self._assets = assets or []
        self.count = len(self._assets) if count is None else count

    def current_page(self) -> list[FakeAsset]:
        return self._assets


class FakeAssetApi:
    """``client.asset``, recording every request so a test can count the calls."""

    def __init__(self, behavior: Callable[[Any], Any]) -> None:
        self._behavior = behavior
        self.requests: list[Any] = []

    async def search(self, request: Any) -> Any:
        self.requests.append(request)
        return self._behavior(request)


class FakeAtlasClient:
    """What :func:`~application_sdk.testing.harness.atlas.atlas_client` yields."""

    def __init__(self, behavior: Callable[[Any], Any]) -> None:
        self.asset = FakeAssetApi(behavior)

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
