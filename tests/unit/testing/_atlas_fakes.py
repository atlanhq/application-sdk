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

Child H added the caches, ``user`` and ``save`` for the same reason: resolving
the ``$admin`` ACL and creating the ephemeral connection are Atlas calls that
used to go through a *second*, synchronous pyatlan client built inside
``BaseE2ETest``. There is one client now, so there is one double.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
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
        self.saved: list[Any] = []

    async def search(self, request: Any) -> Any:
        self.requests.append(request)
        return self._behavior(request)

    async def purge_by_guid(self, guid: Any) -> None:
        self.purged.append(guid)
        if self._on_purge is not None:
            self._on_purge(guid)

    async def save(self, asset: Any) -> None:
        self.saved.append(asset)


class FakeCache:
    """One of pyatlan's async caches, as ``create_connection`` uses them.

    Both verbs matter and they are different jobs. ``get_id_for_name`` is the
    ``$admin`` lookup :func:`~application_sdk.testing.harness.atlas.admin_identity`
    makes; the ``validate_*`` verbs are the three checks
    ``Connection.creator`` performs and
    :func:`~application_sdk.testing.harness.atlas.create_connection` keeps, so a
    typo in an ACL fails naming the bad value rather than as an Atlas rejection
    naming the whole request.

    Args:
        ids: Role/user name -> id, for the name lookup.
        error: Raised by every method, for the unreadable-cache case.
    """

    def __init__(
        self,
        ids: dict[str, str] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._ids = ids or {}
        self._error = error
        self.validated: list[Any] = []

    async def get_id_for_name(self, name: str) -> str | None:
        if self._error is not None:
            raise self._error
        return self._ids.get(name)

    async def validate_idstrs(self, idstrs: Any) -> None:
        self.validated.append(idstrs)

    async def validate_names(self, names: Any) -> None:
        self.validated.append(names)

    async def validate_aliases(self, aliases: Any) -> None:
        self.validated.append(aliases)


class FakeUserApi:
    """``client.user``, for the current-identity half of the admin read."""

    def __init__(
        self, username: str | None = "svc", *, error: BaseException | None = None
    ) -> None:
        self._username = username
        self._error = error

    async def get_current(self) -> Any:
        if self._error is not None:
            raise self._error
        if self._username is None:
            return None
        return SimpleNamespace(username=self._username)


class FakeAtlasClient:
    """What :func:`~application_sdk.testing.harness.atlas.atlas_client` yields.

    Args:
        behavior: What ``asset.search`` answers.
        on_purge: Called with each ``purge_by_guid`` argument.
        roles: Role name -> GUID, for the ``$admin`` lookup.
        username: Who ``user.get_current`` reports, or ``None`` for nobody.
        identity_error: Raised by both halves of the admin read.
    """

    def __init__(
        self,
        behavior: Callable[[Any], Any],
        *,
        on_purge: Callable[[Any], None] | None = None,
        roles: dict[str, str] | None = None,
        username: str | None = "svc",
        identity_error: BaseException | None = None,
    ) -> None:
        self.asset = FakeAssetApi(behavior, on_purge=on_purge)
        self.role_cache = FakeCache(roles, error=identity_error)
        self.user_cache = FakeCache()
        self.group_cache = FakeCache()
        self.user = FakeUserApi(username, error=identity_error)

    @property
    def searches(self) -> int:
        """How many searches this one client served — the reuse assertion."""
        return len(self.asset.requests)

    @property
    def saved(self) -> list[Any]:
        """Every asset written through ``asset.save``, in order."""
        return self.asset.saved


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
