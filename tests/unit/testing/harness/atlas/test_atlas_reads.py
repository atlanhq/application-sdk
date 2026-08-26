"""The Atlas half of the ``client.py`` split: what a read answers, and how often.

Two claims are load-bearing here and neither was true before FND-242.

**A failed read is not an empty one.** Every count and sample method returned
zeros or ``[]`` on a search error, so an Atlas outage surfaced as "asset floor
not met" and sent the reader to the connector. These readers return
:class:`~application_sdk.testing.harness.outcome.Indeterminate`, and the tests
below pin both halves of the distinction — a settled zero stays a zero.

**One client, not one per probe.** The five ``asyncio.run`` bridges stood up a
fresh event loop and a fresh ``AsyncAtlanClient`` per call, so a 1,500s poll at
the 30s default paid up to ~50 TLS handshakes for one boolean.
:attr:`FakeAtlasClientCM.opens` is what makes that assertable rather than
asserted in prose.

The real ``FluentSearch`` request-building runs throughout; only
``asset.search`` is faked.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from application_sdk.testing.harness import atlas
from application_sdk.testing.harness._poll import fake_clock
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import (
    Expired,
    Indeterminate,
    NeverStarted,
    Settled,
)
from tests.unit.testing._atlas_fakes import (
    FakeAsset,
    FakeAtlasClient,
    FakeSearchResult,
    fake_atlas,
)

_QN = "default/mysql/1700000000"


def _client(behavior: Any) -> FakeAtlasClient:
    return FakeAtlasClient(behavior)


def _boom(_request: Any) -> FakeSearchResult:
    raise RuntimeError("atlas is down")


def _budget(**overrides: Any) -> Budget:
    base: dict[str, Any] = {
        "timeout": timedelta(seconds=300),
        "poll_interval": timedelta(seconds=30),
        "start_grace": timedelta(seconds=90),
        "max_transient_failures": 4,
        "heartbeat": None,
    }
    return Budget(**{**base, **overrides})


# ---------------------------------------------------------------------------
# A failed read is Indeterminate, a genuine zero is Settled
# ---------------------------------------------------------------------------


class TestUnreadableIsNotEmpty:
    """The C4 fix, one reader at a time."""

    async def test_counts_report_no_verdict_when_the_search_fails(self) -> None:
        outcome = await atlas.count_assets(_client(_boom), _QN, ("Table", "View"))
        assert isinstance(outcome, Indeterminate)
        assert isinstance(outcome.cause, RuntimeError)
        assert _QN in outcome.label

    async def test_a_readable_zero_is_a_settled_zero(self) -> None:
        outcome = await atlas.count_assets(
            _client(lambda _r: FakeSearchResult(count=0)), _QN, ("Table",)
        )
        assert isinstance(outcome, Settled)
        assert outcome.value == {"Table": 0}

    async def test_one_failing_type_makes_the_whole_mapping_unreadable(self) -> None:
        """A mapping with four real numbers and one silent zero is the fail-open
        bug in miniature: it grades as a real reading, and the zero is the one
        an expectation trips on."""
        seen: list[int] = []

        def behavior(_request: Any) -> FakeSearchResult:
            seen.append(1)
            if len(seen) == 2:
                raise RuntimeError("this type's search failed")
            return FakeSearchResult(count=7)

        outcome = await atlas.count_assets(
            _client(behavior), _QN, ("Database", "Schema", "Table")
        )
        assert isinstance(outcome, Indeterminate)

    async def test_total_count_reports_no_verdict_when_the_search_fails(self) -> None:
        outcome = await atlas.count_total_assets(_client(_boom), _QN)
        assert isinstance(outcome, Indeterminate)

    async def test_lineage_counts_report_no_verdict_when_the_search_fails(
        self,
    ) -> None:
        outcome = await atlas.count_lineage(_client(_boom), _QN, ("Table",))
        assert isinstance(outcome, Indeterminate)

    async def test_samples_report_no_verdict_when_the_search_fails(self) -> None:
        """The sample path fails *open* today — an empty list makes the location
        check skip the type, a silent pass. That is the one this most needed."""
        outcome = await atlas.sample_qualified_names(
            _client(_boom), _QN, ("Table",), per_type=3
        )
        assert isinstance(outcome, Indeterminate)

    async def test_connection_probe_separates_not_visible_from_could_not_look(
        self,
    ) -> None:
        absent = await atlas.connection_exists(
            _client(lambda _r: FakeSearchResult(count=0)), _QN
        )
        assert isinstance(absent, Settled)
        assert absent.value is False

        unreadable = await atlas.connection_exists(_client(_boom), _QN)
        assert isinstance(unreadable, Indeterminate)


# ---------------------------------------------------------------------------
# What the readers actually read
# ---------------------------------------------------------------------------


class TestReadShapes:
    async def test_counts_map_each_type_to_its_own_search(self) -> None:
        counts = iter([3, 5, 8])

        def behavior(_request: Any) -> FakeSearchResult:
            return FakeSearchResult(count=next(counts))

        client = _client(behavior)
        outcome = await atlas.count_assets(client, _QN, ("Database", "Schema", "Table"))
        assert isinstance(outcome, Settled)
        assert outcome.value == {"Database": 3, "Schema": 5, "Table": 8}
        assert client.searches == 3

    async def test_no_type_names_counts_nothing_rather_than_everything(self) -> None:
        """An empty request is an empty answer, never the unfiltered total."""
        client = _client(lambda _r: FakeSearchResult(count=999))
        outcome = await atlas.count_assets(client, _QN, ())
        assert isinstance(outcome, Settled)
        assert outcome.value == {}
        assert client.searches == 0

    async def test_the_total_count_takes_one_search_with_no_type_filter(self) -> None:
        client = _client(lambda _r: FakeSearchResult(count=42))
        outcome = await atlas.count_total_assets(client, _QN)
        assert isinstance(outcome, Settled)
        assert outcome.value == 42
        assert client.searches == 1

    async def test_no_type_names_samples_nothing(self) -> None:
        client = _client(lambda _r: FakeSearchResult([FakeAsset(f"{_QN}/t")]))
        outcome = await atlas.sample_qualified_names(client, _QN, (), per_type=3)
        assert isinstance(outcome, Settled)
        assert outcome.value == {}
        assert client.searches == 0

    async def test_samples_are_capped_at_per_type(self) -> None:
        client = _client(
            lambda _r: FakeSearchResult([FakeAsset(f"{_QN}/t{i}") for i in range(10)])
        )
        outcome = await atlas.sample_qualified_names(
            client, _QN, ("Table",), per_type=3
        )
        assert isinstance(outcome, Settled)
        assert len(outcome.value["Table"]) == 3

    async def test_lineage_counts_add_the_has_lineage_filter(self) -> None:
        """The one thing that separates this from ``count_assets``: it has to
        reach the wire, or the "Lineage coverage" card is just the asset count.
        """
        client = _client(lambda _r: FakeSearchResult(count=1))
        await atlas.count_lineage(client, _QN, ("Table",))
        plain = _client(lambda _r: FakeSearchResult(count=1))
        await atlas.count_assets(plain, _QN, ("Table",))
        assert repr(client.asset.requests[0].dsl) != repr(plain.asset.requests[0].dsl)


# ---------------------------------------------------------------------------
# The poll, and the handshake count that motivated the whole split
# ---------------------------------------------------------------------------


class TestPollForConnection:
    async def test_one_client_serves_the_whole_poll(self) -> None:
        """The main prize. Before the split this was one client — and one TLS
        handshake, and one event loop — per probe."""
        probes: list[int] = []

        def behavior(_request: Any) -> FakeSearchResult:
            probes.append(1)
            return FakeSearchResult(count=1 if len(probes) >= 4 else 0)

        cm = fake_atlas(behavior)
        async with cm as client:
            with fake_clock():
                outcome = await atlas.poll_for_connection(client, _QN, budget=_budget())
        assert isinstance(outcome, Settled)
        assert outcome.value is True
        assert len(probes) == 4
        assert cm.opens == 1
        assert client.searches == 4

    async def test_an_empty_streak_is_never_started_not_expired(self) -> None:
        """ "The connection never appeared" is a claim about dispatch, not about
        the wait being too short — and the diagnostic the old ``False`` return
        threw away."""
        client = _client(lambda _r: FakeSearchResult(count=0))
        with fake_clock():
            outcome = await atlas.poll_for_connection(client, _QN, budget=_budget())
        assert isinstance(outcome, NeverStarted)
        assert outcome.grace == timedelta(seconds=90)
        # Four probes: t=0, 30, 60, 90 — the grace is checked after a reading,
        # so the one that closes it is the one at t=90.
        assert outcome.attempts == 4

    async def test_an_outage_is_indeterminate_not_a_missing_connection(self) -> None:
        """The whole point: an Atlas fault must not read as "publish never
        landed the Connection", which is where the old code sent the reader."""
        with fake_clock():
            outcome = await atlas.poll_for_connection(
                _client(_boom), _QN, budget=_budget()
            )
        assert isinstance(outcome, Indeterminate)
        assert outcome.transient_failures == 4

    async def test_the_grace_can_be_disabled_to_reach_the_ceiling(self) -> None:
        client = _client(lambda _r: FakeSearchResult(count=0))
        with fake_clock():
            outcome = await atlas.poll_for_connection(
                client, _QN, budget=_budget(start_grace=None)
            )
        assert isinstance(outcome, Expired)

    async def test_a_bug_in_the_probe_is_not_absorbed_as_a_blip(self) -> None:
        """Only the unreadable-search signal is transient. A wiring bug raises
        the same exception every attempt, so waiting out a 25-minute budget on
        it would only delay the failure."""

        async def broken(_client: Any, _qn: str) -> Any:
            raise TypeError("connection_exists was called wrongly")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(atlas, "connection_exists", broken)
            with pytest.raises(TypeError), fake_clock():
                await atlas.poll_for_connection(_client(_boom), _QN, budget=_budget())


# ---------------------------------------------------------------------------
# The client factory
# ---------------------------------------------------------------------------


class TestAtlasClient:
    async def test_oauth_identity_wins_when_both_are_configured(self) -> None:
        """A service account with realm-admin can be missing from an asset ACL
        the OAuth client *is* on; that choice is the whole reason the factory
        exists, so which credential it picks is worth pinning."""
        built: list[dict[str, Any]] = []

        class _Recorder:
            def __init__(self, **kwargs: Any) -> None:
                built.append(kwargs)

            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_exc: Any) -> bool:
                return False

        import pyatlan.client.aio.client as aio

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(aio, "AsyncAtlanClient", _Recorder)
            async with atlas.atlas_client(
                "https://x.atlan.com",
                "tok",
                oauth_client_id="cid",
                oauth_client_secret="secret",
            ):
                pass
            async with atlas.atlas_client("https://x.atlan.com", "tok"):
                pass

        assert built[0]["oauth_client_id"] == "cid"
        assert "api_key" not in built[0]
        assert built[1]["api_key"] == "tok"
        assert "oauth_client_id" not in built[1]
