"""The preview hold: keep a passing run's connection for review, reap it later.

The two properties that matter are pinned here: a connection is only ever
reaped when it carries the hold marker AND has the harness's own minted
qualified-name shape, and an expiry that has not passed is left alone.
Synthetic identifiers only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from application_sdk.testing.harness import preview_hold
from tests.unit.testing._atlas_fakes import FakeAtlasClient, FakeSearchResult

_NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=UTC)
_HELD_QN = "default/sigma/1700000000123456"


class _Hit:
    def __init__(self, qualified_name: str, description: str | None) -> None:
        self.qualified_name = qualified_name
        self.description = description
        self.guid = f"guid-{qualified_name}"


def _client(hits: list[_Hit]) -> FakeAtlasClient:
    return FakeAtlasClient(lambda _r: FakeSearchResult(hits))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "hours"),
    [("", 0), ("abc", 0), ("-3", 0), ("24", 24), ("9999", preview_hold.MAX_HOLD_HOURS)],
)
def test_hold_hours_is_zero_unless_asked_and_is_capped(raw: str, hours: int) -> None:
    assert preview_hold.hold_hours({preview_hold.HOLD_ENV: raw}) == hours


def test_a_note_round_trips_to_its_expiry() -> None:
    assert preview_hold.held_until(preview_hold.hold_note(_NOW)) == _NOW
    assert preview_hold.held_until("a normal connection") is None


@pytest.mark.parametrize(
    ("qn", "ephemeral"),
    [
        (_HELD_QN, True),
        ("default/snowflake/1700000000", False),
        ("default/sigma/1700000000123456/child", False),
        ("prod/sigma/1700000000123456", False),
    ],
)
def test_only_harness_minted_names_count_as_ephemeral(qn: str, ephemeral: bool) -> None:
    assert preview_hold.is_ephemeral(qn) is ephemeral


async def test_only_expired_holds_on_harness_connections_are_due() -> None:
    past = preview_hold.hold_note(datetime(2026, 1, 1, tzinfo=UTC))
    future = preview_hold.hold_note(datetime(2026, 1, 3, tzinfo=UTC))
    client = _client(
        [
            _Hit(_HELD_QN, past),
            _Hit("default/sigma/1700000000999999", future),
            _Hit("default/snowflake/1700000000", past),
            _Hit("default/sigma/1700000000777777", "no marker here"),
        ]
    )
    assert await preview_hold.expired(client, _NOW) == [_HELD_QN]


async def test_reap_purges_what_is_due_and_reports_only_clean_purges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "default/sigma/1700000000888888"
    past = preview_hold.hold_note(datetime(2026, 1, 1, tzinfo=UTC))
    purged: list[str] = []

    async def _purge(_client: Any, qn: str) -> SimpleNamespace:
        purged.append(qn)
        return SimpleNamespace(errors=("boom",) if qn == other else ())

    monkeypatch.setattr(preview_hold, "purge_connection", _purge)
    client = _client([_Hit(_HELD_QN, past), _Hit(other, past)])
    assert await preview_hold.reap(client, _NOW) == [_HELD_QN]
    assert purged == [_HELD_QN, other]


async def test_mark_saves_the_note_on_the_connection() -> None:
    client = _client([])
    await preview_hold.mark(client, _HELD_QN, "sigma-1700000000123456", _NOW)
    [saved] = client.saved
    assert saved.qualified_name == _HELD_QN
    assert preview_hold.held_until(saved.description) == _NOW


# ---------------------------------------------------------------------------
# A hold belongs to its PR and suite
# ---------------------------------------------------------------------------

_PR = "example-org/example-app#42"
_SUITE = "ExampleE2E"
_LATER = datetime(2026, 1, 3, tzinfo=UTC)


def test_a_note_carries_the_pr_and_suite_it_was_held_for() -> None:
    hold = preview_hold.parse_hold(
        _HELD_QN, preview_hold.hold_note(_NOW, pr=_PR, suite=_SUITE)
    )
    assert hold == preview_hold.Hold(_HELD_QN, _NOW, pr=_PR, suite=_SUITE)


def test_a_note_without_a_pr_still_parses_as_a_hold() -> None:
    """Holds written before the PR was recorded must still be reaped."""
    hold = preview_hold.parse_hold(
        _HELD_QN, f"{preview_hold.MARKER} expires=2026-01-02T12:00:00Z"
    )
    assert hold == preview_hold.Hold(_HELD_QN, _NOW)


@pytest.mark.parametrize(
    ("raw", "owner"),
    [
        (_PR, _PR),
        ("", None),
        ("example-org/example-app", None),
        ("example-org/example-app#42/../x", None),
        ("../../etc#1", None),
    ],
)
def test_only_an_owner_repo_number_pr_is_recorded(raw: str, owner: str | None) -> None:
    assert preview_hold.hold_owner({preview_hold.PR_ENV: raw}) == owner


def _recording_purge(monkeypatch: pytest.MonkeyPatch, failing: str = "") -> list[str]:
    purged: list[str] = []

    async def _purge(_client: Any, qn: str) -> SimpleNamespace:
        purged.append(qn)
        return SimpleNamespace(errors=("boom",) if qn == failing else ())

    monkeypatch.setattr(preview_hold, "purge_connection", _purge)
    return purged


async def test_a_new_hold_replaces_the_prs_earlier_hold_for_the_same_suite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    earlier = "default/sigma/1700000000000001"
    other_suite = "default/sigma/1700000000000002"
    other_pr = "default/sigma/1700000000000003"
    purged = _recording_purge(monkeypatch)
    client = _client(
        [
            _Hit(earlier, preview_hold.hold_note(_LATER, pr=_PR, suite=_SUITE)),
            _Hit(other_suite, preview_hold.hold_note(_LATER, pr=_PR, suite="OtherE2E")),
            _Hit(
                other_pr,
                preview_hold.hold_note(
                    _LATER, pr="example-org/example-app#7", suite=_SUITE
                ),
            ),
        ]
    )
    held = await preview_hold.hold(
        client, _HELD_QN, "sigma-1", _LATER, pr=_PR, suite=_SUITE, now=_NOW
    )
    assert held is True
    assert [s.qualified_name for s in client.saved] == [_HELD_QN]
    assert purged == [earlier]


async def test_a_hold_is_refused_once_the_tenant_is_at_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged = _recording_purge(monkeypatch)
    full = [
        _Hit(
            f"default/sigma/17000000000{i:05d}",
            preview_hold.hold_note(
                _LATER, pr=f"example-org/example-app#{i}", suite=_SUITE
            ),
        )
        for i in range(preview_hold.MAX_HOLDS_PER_TENANT)
    ]
    client = _client(full)
    held = await preview_hold.hold(
        client, _HELD_QN, "sigma-1", _LATER, pr=_PR, suite=_SUITE, now=_NOW
    )
    assert held is False
    assert client.saved == []
    assert purged == []


async def test_expired_holds_do_not_count_toward_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _recording_purge(monkeypatch)
    past = datetime(2026, 1, 1, tzinfo=UTC)
    client = _client(
        [
            _Hit(f"default/sigma/17000000000{i:05d}", preview_hold.hold_note(past))
            for i in range(preview_hold.MAX_HOLDS_PER_TENANT)
        ]
    )
    assert await preview_hold.hold(
        client, _HELD_QN, "sigma-1", _LATER, pr=_PR, suite=_SUITE, now=_NOW
    )


async def test_the_reaper_purges_an_unexpired_hold_once_its_pr_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    still_open = "default/sigma/1700000000000004"
    open_pr = "example-org/example-app#8"
    purged = _recording_purge(monkeypatch)
    asked: list[set[str]] = []

    async def _closed(prs: set[str]) -> set[str]:
        asked.append(prs)
        return {_PR}

    client = _client(
        [
            _Hit(_HELD_QN, preview_hold.hold_note(_LATER, pr=_PR, suite=_SUITE)),
            _Hit(still_open, preview_hold.hold_note(_LATER, pr=open_pr, suite=_SUITE)),
        ]
    )
    assert await preview_hold.reap(client, _NOW, pr_closed=_closed) == [_HELD_QN]
    assert purged == [_HELD_QN]
    assert asked == [{_PR, open_pr}]


async def test_without_a_pr_lookup_only_expiry_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purged = _recording_purge(monkeypatch)
    client = _client([_Hit(_HELD_QN, preview_hold.hold_note(_LATER, pr=_PR))])
    assert await preview_hold.reap(client, _NOW) == []
    assert purged == []


async def test_closed_prs_reads_state_and_counts_an_unreadable_pr_as_open() -> None:
    import httpx

    def _respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/pulls/42"):
            return httpx.Response(200, json={"state": "closed"})
        if request.url.path.endswith("/pulls/8"):
            return httpx.Response(200, json={"state": "open"})
        return httpx.Response(404, json={})

    closed = await preview_hold.closed_prs(
        {_PR, "example-org/example-app#8", "example-org/example-app#9"},
        "test-token",
        transport=httpx.MockTransport(_respond),
    )
    assert closed == {_PR}
