"""``BaseE2ETest.seed_glossary``: a seeded glossary and its terms, and their teardown.

A glossary belongs to no connection, so the connection delete in teardown never
reaches it. The seed therefore registers every GUID it creates on a
kind-agnostic list, and teardown purges that list newest first. Pinned here, in
three layers:

* the Atlas writes (``create_glossary`` / ``create_glossary_term``): what is
  saved, and that the GUID comes from the save response's ``guidAssignments``;
* the purge (``purge_assets``): one purge per GUID, in the order given, and
  nothing raises;
* the wiring on ``BaseE2ETest``: every GUID is registered as soon as Atlas
  returns it, and teardown purges terms before their glossary whatever the
  run's outcome — a failed run, a failed seed and a failed connection delete
  included.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pyatlan.errors import NotFoundError
from pyatlan.model.assets import AtlasGlossary, AtlasGlossaryTerm
from pyatlan.model.enums import CertificateStatus

from application_sdk.errors.leaves import InvalidInputError
from application_sdk.testing.e2e._errors import SeededAssetsNotSearchableError
from application_sdk.testing.e2e.base import BaseE2ETest
from application_sdk.testing.e2e.payload import RunMode
from application_sdk.testing.e2e.substitutions import MustacheSubstitutions
from application_sdk.testing.harness import atlas
from application_sdk.testing.harness.budgets import Budget
from application_sdk.testing.harness.outcome import Indeterminate, Settled
from application_sdk.testing.harness.teardown import purge_assets

_GLOSSARY_GUID = "guid-glossary"


class _FakeAssetApi:
    """``client.asset``: ``save`` answers with a GUID per placeholder.

    Args:
        guids: Real GUIDs to hand out, in save order. One per saved asset.
        save_errors: Save ordinal (1-based) -> exception to raise instead.
        purge_errors: GUID -> exception ``purge_by_guid`` raises for it.
        omit_assignment: When set, saves answer with no ``guidAssignments``.
        search_counts: What successive searches report as ``count``; the last
            one repeats. An exception in the list is raised instead. ``None``
            reports every successful save as visible.
    """

    def __init__(
        self,
        guids: list[str] | None = None,
        *,
        save_errors: dict[int, BaseException] | None = None,
        purge_errors: dict[str, BaseException] | None = None,
        omit_assignment: bool = False,
        search_counts: list[int | BaseException] | None = None,
    ) -> None:
        self._guids = list(guids or [])
        self._save_errors = save_errors or {}
        self._purge_errors = purge_errors or {}
        self._omit_assignment = omit_assignment
        self._search_counts = search_counts
        self._created = 0
        self.saved: list[Any] = []
        self.purged: list[Any] = []
        self.searches: list[Any] = []

    async def search(self, request: Any) -> Any:
        self.searches.append(request)
        if self._search_counts is None:
            return SimpleNamespace(count=self._created)
        answer = (
            self._search_counts.pop(0)
            if len(self._search_counts) > 1
            else self._search_counts[0]
        )
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(count=answer)

    async def save(self, asset: Any) -> Any:
        self.saved.append(asset)
        error = self._save_errors.get(len(self.saved))
        if error is not None:
            raise error
        if self._omit_assignment:
            return SimpleNamespace(guid_assignments=None)
        self._created += 1
        return SimpleNamespace(guid_assignments={asset.guid: self._guids.pop(0)})

    async def purge_by_guid(self, guid: Any) -> None:
        self.purged.append(guid)
        error = self._purge_errors.get(guid)
        if error is not None:
            raise error


def _client(**kwargs: Any) -> Any:
    return SimpleNamespace(asset=_FakeAssetApi(**kwargs))


def _terms() -> list[atlas.GlossaryTermSpec]:
    return [
        atlas.GlossaryTermSpec(name="Revenue", description="Money in", verified=True),
        atlas.GlossaryTermSpec(name="Churn"),
    ]


def _not_found() -> NotFoundError:
    return NotFoundError(
        SimpleNamespace(
            http_error_code=404,
            error_id="ATLAN-JAVA-404-000",
            error_message="not found",
            user_action="",
        )
    )


# ---------------------------------------------------------------------------
# Atlas writes
# ---------------------------------------------------------------------------


class TestCreateGlossary:
    async def test_the_glossary_is_saved_under_its_name_and_its_guid_returned(
        self,
    ) -> None:
        client = _client(guids=[_GLOSSARY_GUID])
        guid = await atlas.create_glossary(client, name="e2e-glean-1")
        assert guid == _GLOSSARY_GUID
        [saved] = client.asset.saved
        assert isinstance(saved, AtlasGlossary)
        assert saved.name == "e2e-glean-1"
        # A placeholder, which Atlas swaps for the real GUID it reports back.
        assert saved.guid.startswith("-")

    async def test_a_response_without_the_guid_fails_instead_of_untracked(
        self,
    ) -> None:
        client = _client(omit_assignment=True)
        with pytest.raises(atlas.SeedWriteUnconfirmedError):
            await atlas.create_glossary(client, name="e2e-glean-1")


class TestCreateGlossaryTerm:
    async def test_a_verified_term_is_anchored_described_and_certified(
        self,
    ) -> None:
        client = _client(guids=["guid-revenue"])
        guid = await atlas.create_glossary_term(
            client, glossary_guid=_GLOSSARY_GUID, term=_terms()[0]
        )
        assert guid == "guid-revenue"
        [saved] = client.asset.saved
        assert isinstance(saved, AtlasGlossaryTerm)
        assert saved.name == "Revenue"
        assert saved.anchor.guid == _GLOSSARY_GUID
        assert saved.description == "Money in"
        assert saved.certificate_status == CertificateStatus.VERIFIED

    async def test_an_unverified_term_carries_no_certificate(self) -> None:
        client = _client(guids=["guid-churn"])
        await atlas.create_glossary_term(
            client, glossary_guid=_GLOSSARY_GUID, term=_terms()[1]
        )
        [saved] = client.asset.saved
        assert saved.certificate_status is None
        assert saved.description is None

    async def test_a_response_without_the_guid_fails(self) -> None:
        client = _client(omit_assignment=True)
        with pytest.raises(atlas.SeedWriteUnconfirmedError):
            await atlas.create_glossary_term(
                client, glossary_guid=_GLOSSARY_GUID, term=_terms()[1]
            )


class TestCountAssetsByGuid:
    async def test_the_count_comes_from_one_guid_filtered_search(self) -> None:
        client = _client(search_counts=[2])
        reading = await atlas.count_assets_by_guid(client, ["g", "t1", "t2"])
        assert isinstance(reading, Settled)
        assert reading.value == 2
        [request] = client.asset.searches
        assert "t1" in str(request.dsl.query)
        assert request.dsl.size == 0

    async def test_an_unreadable_search_is_no_verdict_not_zero(self) -> None:
        client = _client(search_counts=[RuntimeError("index down")])
        reading = await atlas.count_assets_by_guid(client, ["g"])
        assert isinstance(reading, Indeterminate)


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


class TestPurgeAssets:
    async def test_each_guid_is_purged_once_in_the_order_given(self) -> None:
        client = _client()
        report = await purge_assets(client, ["t2", "t1", "g"])
        assert client.asset.purged == ["t2", "t1", "g"]
        assert report.complete
        assert report.purged == 3

    async def test_one_failure_does_not_stop_the_rest_and_nothing_raises(
        self,
    ) -> None:
        client = _client(purge_errors={"t1": RuntimeError("boom")})
        report = await purge_assets(client, ["t2", "t1", "g"])
        assert client.asset.purged == ["t2", "t1", "g"]
        assert not report.complete
        assert report.purged == 2
        assert list(report.orphaned) == ["t1"]
        assert len(report.errors) == 1

    async def test_an_asset_already_gone_counts_as_purged(self) -> None:
        client = _client(purge_errors={"g": _not_found()})
        report = await purge_assets(client, ["g"])
        assert report.complete
        assert report.purged == 1

    async def test_nothing_to_purge_is_complete(self) -> None:
        client = _client()
        report = await purge_assets(client, [])
        assert client.asset.purged == []
        assert report.complete


# ---------------------------------------------------------------------------
# BaseE2ETest wiring
# ---------------------------------------------------------------------------


class _GlossaryE2ETest(BaseE2ETest):
    """Minimal concrete subclass, built without ``setup_method``."""

    connector_short_name = "glean"
    argo_package_name = "@atlan/glean"
    argo_template_name = "atlan-glean"
    mode = RunMode.DIRECT
    app_service_url = "http://glean.svc"

    def _mustache_substitutions(self) -> MustacheSubstitutions:  # pragma: no cover
        raise NotImplementedError


def _harness(
    monkeypatch: pytest.MonkeyPatch,
    client: Any,
    *,
    client_error: BaseException | None = None,
) -> tuple[_GlossaryE2ETest, list[str]]:
    """A harness whose Atlas client is *client*, and the warnings it logged.

    The run submitted nothing and created no connection, so teardown's
    connection delete has nothing to do: what is left is the seeded-asset purge.

    Args:
        monkeypatch: pytest's patcher.
        client: What ``_atlas_client`` yields.
        client_error: Raised on entering ``_atlas_client`` instead, for an
            unreachable tenant.
    """
    warnings: list[str] = []

    @asynccontextmanager
    async def _atlas_client(_self: Any) -> AsyncIterator[Any]:
        if client_error is not None:
            raise client_error
        yield client

    def _record_warning(message: str, *args: object, **_kwargs: object) -> None:
        warnings.append(message % args if args else message)

    def _ignore(*_args: object, **_kwargs: object) -> None:
        return None

    harness = _GlossaryE2ETest()
    harness.connection_qualified_name = "default/glean/1"
    harness._submitted_connection_qns = []
    harness._seeded_connection_qns = []
    harness._seeded_prefixes = {}
    harness._seeded_asset_guids = []
    harness._minter = SimpleNamespace(unique_suffix=lambda: "1787587123106596")
    monkeypatch.setattr(_GlossaryE2ETest, "_atlas_client", _atlas_client)
    monkeypatch.setattr(
        _GlossaryE2ETest,
        "_seed_readback_budget",
        lambda _self: Budget(
            timeout=timedelta(seconds=0.3),
            poll_interval=timedelta(seconds=0.01),
            heartbeat=None,
        ),
    )
    monkeypatch.setattr(
        "application_sdk.testing.e2e.base.logger",
        SimpleNamespace(info=_ignore, warning=_record_warning, error=_record_warning),
    )
    return harness, warnings


class TestSeedGlossary:
    def test_the_glossary_name_is_run_unique_and_every_guid_is_registered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"])
        harness, _warnings = _harness(monkeypatch, client)
        seeded = harness.seed_glossary(_terms())
        assert seeded == atlas.SeededGlossary(
            name="e2e-glean-1787587123106596",
            guid=_GLOSSARY_GUID,
            term_guids={"Revenue": "guid-revenue", "Churn": "guid-churn"},
        )
        assert harness._seeded_asset_guids == [
            _GLOSSARY_GUID,
            "guid-revenue",
            "guid-churn",
        ]

    @pytest.mark.parametrize(
        "terms",
        [
            [],
            [atlas.GlossaryTermSpec(name="A"), atlas.GlossaryTermSpec(name="A")],
        ],
        ids=["empty", "duplicate-names"],
    )
    def test_bad_terms_are_rejected_before_anything_is_written(
        self, monkeypatch: pytest.MonkeyPatch, terms: list[atlas.GlossaryTermSpec]
    ) -> None:
        client = _client(guids=[_GLOSSARY_GUID])
        harness, _warnings = _harness(monkeypatch, client)
        with pytest.raises(InvalidInputError):
            harness.seed_glossary(terms)
        assert client.asset.saved == []
        assert harness._seeded_asset_guids == []


class TestSeedGlossaryWaitsForSearch:
    def test_it_returns_once_search_returns_every_seeded_asset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(
            guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"],
            search_counts=[0, 1, 3],
        )
        harness, _warnings = _harness(monkeypatch, client)
        harness.seed_glossary(_terms())
        assert len(client.asset.searches) == 3
        assert all(
            guid in str(client.asset.searches[0].dsl.query)
            for guid in (_GLOSSARY_GUID, "guid-revenue", "guid-churn")
        )

    def test_an_unreadable_search_keeps_waiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(
            guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"],
            search_counts=[RuntimeError("index down"), 3],
        )
        harness, _warnings = _harness(monkeypatch, client)
        harness.seed_glossary(_terms())
        assert len(client.asset.searches) == 2

    def test_it_times_out_with_a_clear_error_and_teardown_still_purges(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(
            guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"],
            search_counts=[2],
        )
        harness, _warnings = _harness(monkeypatch, client)
        with pytest.raises(SeededAssetsNotSearchableError, match="2 of 3"):
            harness.seed_glossary(_terms())
        harness.teardown_method(method=None)
        assert client.asset.purged == ["guid-churn", "guid-revenue", _GLOSSARY_GUID]


class TestTeardownPurgesTheSeededGlossary:
    def test_terms_are_purged_before_their_glossary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"])
        harness, warnings = _harness(monkeypatch, client)
        harness.seed_glossary(_terms())
        harness.teardown_method(method=None)
        assert client.asset.purged == ["guid-churn", "guid-revenue", _GLOSSARY_GUID]
        assert harness._seeded_asset_guids == []
        assert warnings == []

    def test_a_failed_run_still_purges_the_seed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pytest calls ``teardown_method`` after a failed test too; nothing in
        the purge depends on the run's verdict."""
        client = _client(guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"])
        harness, _warnings = _harness(monkeypatch, client)
        harness.seed_glossary(_terms())
        with pytest.raises(AssertionError):
            raise AssertionError("the DAG failed")
        harness.teardown_method(method=None)
        assert client.asset.purged == ["guid-churn", "guid-revenue", _GLOSSARY_GUID]

    def test_a_seed_that_fails_part_way_still_has_what_it_created_purged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The glossary and the first term were created; the second term's save
        was rejected. Both created assets are registered and reclaimed."""
        client = _client(
            guids=[_GLOSSARY_GUID, "guid-revenue"],
            save_errors={3: RuntimeError("rejected")},
        )
        harness, _warnings = _harness(monkeypatch, client)
        with pytest.raises(RuntimeError, match="rejected"):
            harness.seed_glossary(_terms())
        harness.teardown_method(method=None)
        assert client.asset.purged == ["guid-revenue", _GLOSSARY_GUID]

    def test_a_failing_connection_delete_does_not_skip_the_purge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"])
        harness, _warnings = _harness(monkeypatch, client)
        harness.seed_glossary(_terms())

        async def _explode(_self: Any) -> None:
            raise RuntimeError("connection delete blew up")

        monkeypatch.setattr(_GlossaryE2ETest, "_delete_connections", _explode)
        with pytest.raises(RuntimeError, match="connection delete blew up"):
            harness.teardown_method(method=None)
        assert client.asset.purged == ["guid-churn", "guid-revenue", _GLOSSARY_GUID]

    def test_a_purge_that_leaves_assets_behind_warns_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _client(
            guids=[_GLOSSARY_GUID, "guid-revenue", "guid-churn"],
            purge_errors={_GLOSSARY_GUID: RuntimeError("still has terms")},
        )
        harness, warnings = _harness(monkeypatch, client)
        harness.seed_glossary(_terms())
        harness.teardown_method(method=None)
        assert any(_GLOSSARY_GUID in warning for warning in warnings)

    def test_an_unreachable_tenant_warns_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, warnings = _harness(
            monkeypatch, _client(), client_error=ConnectionError("no route")
        )
        harness._seeded_asset_guids = [_GLOSSARY_GUID, "guid-revenue"]
        harness.teardown_method(method=None)
        assert any("manual purge" in warning for warning in warnings)

    def test_a_run_that_seeded_nothing_opens_no_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness, warnings = _harness(
            monkeypatch, _client(), client_error=AssertionError("must not open")
        )
        harness.teardown_method(method=None)
        assert warnings == []
