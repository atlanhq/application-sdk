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
