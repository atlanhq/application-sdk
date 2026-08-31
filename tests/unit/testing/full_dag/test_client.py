"""Unit tests for :mod:`application_sdk.testing.full_dag.client`.

Network is monkeypatched out at the async reader's ``_request``; we just verify
the parse + poll-loop logic through the deprecated package's re-export of the
sync ``AEWorkflowClient`` facade.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from application_sdk.testing.e2e.client import _FORBIDDEN_ATTEMPTS_REMOVAL_VERSION
from application_sdk.testing.full_dag._errors import (
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
)
from application_sdk.testing.full_dag.client import (
    AEWorkflowClient,
    DAGNodeStatus,
    DAGRunStatus,
)
from application_sdk.testing.harness._poll import fake_clock
from tests.unit.testing._atlas_fakes import FakeSearchResult, fake_atlas


def _make_client(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, Any]]):
    """Build a client whose AE ``_request`` returns the queued responses in order.

    Patched on ``client._ae`` — the async
    :class:`~application_sdk.testing.harness.automation_engine.client.AEClient`
    the facade delegates to since child F, where ``_request`` lives now.
    """
    client = AEWorkflowClient("https://tenant.example.com/", "fake-token")
    queue = list(responses)

    async def fake_request(method, path, *, body=None, timeout=30, **_kwargs):
        if not queue:
            raise AssertionError("More HTTP calls than queued responses")
        return queue.pop(0)

    monkeypatch.setattr(client._ae, "_request", fake_request)
    return client, queue


def test_submit_workflow_extracts_run_id_from_top_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch, [(200, {"run_id": "abc-123"})])
    assert client.submit_workflow({"any": "payload"}) == "abc-123"


def test_submit_workflow_extracts_run_id_from_nested_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch, [(200, {"data": {"run_id": "nested-xyz"}})])
    assert client.submit_workflow({}) == "nested-xyz"


def test_submit_workflow_raises_on_missing_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(monkeypatch, [(200, {"data": {}})])
    with pytest.raises(AtlanApiResponseInvariantError, match="no run_id"):
        client.submit_workflow({})


def test_submit_workflow_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_client(monkeypatch, [(401, "Unauthorized")])
    with pytest.raises(AtlanApiHttpError, match="HTTP 401"):
        client.submit_workflow({})


def test_get_native_status_parses_dag_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _make_client(
        monkeypatch,
        [
            (
                200,
                {
                    "status": "Running",
                    "run_id": "r-1",
                    "workflow_slug": "mysql-abc",
                    "dag_nodes": {
                        "extract": {
                            "status": "Succeeded",
                            "started_at": 1700000000000,
                            "completed_at": 1700000010000,
                            "error_message": None,
                        },
                        "publish": {
                            "status": "Running",
                            "started_at": 1700000010500,
                            "completed_at": None,
                            "error_message": None,
                        },
                    },
                },
            )
        ],
    )
    result = client.get_native_status("r-1")
    assert result.run_id == "r-1"
    assert result.workflow_slug == "mysql-abc"
    assert result.status is DAGRunStatus.RUNNING
    assert len(result.nodes) == 2
    extract = next(n for n in result.nodes if n.name == "extract")
    assert extract.status is DAGNodeStatus.SUCCEEDED
    assert extract.duration_seconds == 10.0
    publish = next(n for n in result.nodes if n.name == "publish")
    assert publish.status is DAGNodeStatus.RUNNING
    assert publish.duration_seconds is None


def test_unknown_status_strings_treated_as_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(
        monkeypatch,
        [
            (
                200,
                {
                    "status": "MysteryStatus",
                    "dag_nodes": {"x": {"status": "AlsoUnknown"}},
                },
            )
        ],
    )
    result = client.get_native_status("r")
    assert result.status is DAGRunStatus.PENDING
    assert result.nodes[0].status is DAGNodeStatus.PENDING


def test_poll_native_status_returns_on_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, queue = _make_client(
        monkeypatch,
        [
            (200, {"status": "Running", "dag_nodes": {}}),
            (200, {"status": "Running", "dag_nodes": {}}),
            (
                200,
                {
                    "status": "Succeeded",
                    "dag_nodes": {"extract": {"status": "Succeeded"}},
                },
            ),
        ],
    )
    # fake_clock fast-forwards the poll loop's own clock and sleeps; the real
    # time.monotonic (and so the asyncio loop's timers) stay untouched.
    with fake_clock():
        result = client.poll_native_status("r", interval_seconds=1, timeout_seconds=60)
    assert result.status is DAGRunStatus.SUCCEEDED
    assert queue == []  # all three responses consumed


def test_poll_native_status_returns_last_observation_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _make_client(
        monkeypatch,
        # interval=1, timeout=3 → we'll fire 3 polls (elapsed=0,1,2) all Running
        [
            (200, {"status": "Running", "dag_nodes": {}}),
            (200, {"status": "Running", "dag_nodes": {}}),
            (200, {"status": "Running", "dag_nodes": {}}),
        ],
    )
    with fake_clock():
        result = client.poll_native_status("r", interval_seconds=1, timeout_seconds=3)
    # Still Running but we got back the last observed state instead of
    # raising — caller can include node-level diagnostics in their error.
    assert result.status is DAGRunStatus.RUNNING


def test_poll_atlas_for_connection_succeeds_when_search_finds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``poll_atlas_for_connection`` returns True as soon as the
    search-based existence check returns True (indexer may have lagged
    earlier polls)."""
    client, _ = _make_client(monkeypatch, [])

    # Stub the Atlas search itself — first two polls find nothing (indexer lag),
    # the third finds the Connection. The real reader and the real poll loop run.
    calls = {"n": 0}

    def behavior(_request: Any) -> FakeSearchResult:
        calls["n"] += 1
        return FakeSearchResult(count=1 if calls["n"] >= 3 else 0)

    atlas = fake_atlas(behavior)
    monkeypatch.setattr(client, "_atlas", lambda: atlas)
    with fake_clock():
        assert (
            client.poll_atlas_for_connection(
                "default/mysql/test", interval_seconds=1, timeout_seconds=10
            )
            is True
        )
    assert calls["n"] == 3
    # The point of the split: one client and one TLS handshake for the whole
    # poll, where the asyncio.run bridge stood up a fresh one per probe.
    assert atlas.opens == 1


def test_poll_atlas_for_connection_bails_after_not_found_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_not_found_attempts`` consecutive empty searches → False fast."""
    client, _ = _make_client(monkeypatch, [])
    monkeypatch.setattr(
        client, "_atlas", lambda: fake_atlas(lambda _request: FakeSearchResult(count=0))
    )
    with fake_clock():
        assert (
            client.poll_atlas_for_connection(
                "default/mysql/test",
                interval_seconds=1,
                timeout_seconds=1000,
                max_not_found_attempts_override=3,
            )
            is False
        )


def test_max_forbidden_attempts_says_it_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vestigial knob now announces itself rather than being ``del``'d unread.

    The poll reads the search index, whose ACL is permissive, so a 403 never
    surfaces and there is no 403 budget to bound. Deleting the argument unread is
    the shape that lets a caller keep passing a number and believe it does
    something; FND-240 makes it say so, with a named removal version so the
    warning is something to act on.
    """
    client, _ = _make_client(monkeypatch, [])
    monkeypatch.setattr(
        client, "_atlas", lambda: fake_atlas(lambda _request: FakeSearchResult(count=1))
    )
    with fake_clock(), pytest.warns(DeprecationWarning, match="has no effect"):
        assert (
            client.poll_atlas_for_connection(
                "default/mysql/test",
                interval_seconds=1,
                timeout_seconds=10,
                max_forbidden_attempts=5,
            )
            is True
        )


def test_omitting_the_vestigial_knob_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the default became ``None`` rather than staying ``5``: with a real
    default there is no way to tell "the caller asked for this" from "the
    signature did", and every single call would warn."""
    client, _ = _make_client(monkeypatch, [])
    monkeypatch.setattr(
        client, "_atlas", lambda: fake_atlas(lambda _request: FakeSearchResult(count=1))
    )
    with fake_clock(), warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert (
            client.poll_atlas_for_connection(
                "default/mysql/test", interval_seconds=1, timeout_seconds=10
            )
            is True
        )


def test_the_vestigial_knob_names_its_removal_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notice spells "v4.0" literally; this is what stops that drifting.

    Conformance rule B002 reads a deprecation notice as written in the source, so
    interpolating ``_FORBIDDEN_ATTEMPTS_REMOVAL_VERSION`` would make a compliant
    deprecation look like one that never named a removal version. Spelling it
    twice needs one assertion tying them together — the same shape ``AppConfig``'s
    own removal-version test uses.
    """
    client, _ = _make_client(monkeypatch, [])
    monkeypatch.setattr(
        client, "_atlas", lambda: fake_atlas(lambda _request: FakeSearchResult(count=1))
    )
    with fake_clock(), pytest.warns(DeprecationWarning) as caught:
        client.poll_atlas_for_connection(
            "default/mysql/test",
            interval_seconds=1,
            timeout_seconds=10,
            max_forbidden_attempts=5,
        )
    message = str(caught[0].message)
    assert f"removed in v{_FORBIDDEN_ATTEMPTS_REMOVAL_VERSION}" in message
    assert "use max_not_found_attempts instead" in message
