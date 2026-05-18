"""Unit tests for :mod:`application_sdk.testing.full_dag.client`.

Network is monkeypatched out at ``_request``; we just verify the parse
+ poll-loop logic.
"""

from __future__ import annotations

from typing import Any

import pytest

from application_sdk.testing.full_dag._errors import (
    AtlanApiHttpError,
    AtlanApiResponseInvariantError,
)
from application_sdk.testing.full_dag.client import (
    AEWorkflowClient,
    DAGNodeStatus,
    DAGRunStatus,
)


def _make_client(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, Any]]):
    """Build a client whose ``_request`` returns the queued responses in order."""
    client = AEWorkflowClient("https://tenant.example.com/", "fake-token")
    queue = list(responses)

    def fake_request(method, path, *, body=None, timeout=30):
        if not queue:
            raise AssertionError("More HTTP calls than queued responses")
        return queue.pop(0)

    monkeypatch.setattr(client, "_request", fake_request)
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
    # Avoid the real time.sleep — poll interval doesn't matter for the test.
    monkeypatch.setattr("time.sleep", lambda _: None)
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
    result = client.poll_native_status("r", interval_seconds=1, timeout_seconds=60)
    assert result.status is DAGRunStatus.SUCCEEDED
    assert queue == []  # all three responses consumed


def test_poll_native_status_returns_last_observation_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    client, _ = _make_client(
        monkeypatch,
        # interval=1, timeout=3 → we'll fire 3 polls (elapsed=0,1,2) all Running
        [
            (200, {"status": "Running", "dag_nodes": {}}),
            (200, {"status": "Running", "dag_nodes": {}}),
            (200, {"status": "Running", "dag_nodes": {}}),
        ],
    )
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
    monkeypatch.setattr("time.sleep", lambda _: None)
    client, _ = _make_client(monkeypatch, [])

    # Stub the search-based existence check directly — first two polls
    # return False (indexer lag), third returns True (Connection found).
    calls = {"n": 0}

    def fake_search(_qn: str) -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(client, "connection_exists_in_atlas_via_search", fake_search)
    assert (
        client.poll_atlas_for_connection(
            "default/mysql/test", interval_seconds=1, timeout_seconds=10
        )
        is True
    )
    assert calls["n"] == 3


def test_poll_atlas_for_connection_bails_after_not_found_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``max_not_found_attempts`` consecutive empty searches → False fast."""
    monkeypatch.setattr("time.sleep", lambda _: None)
    client, _ = _make_client(monkeypatch, [])
    monkeypatch.setattr(
        client, "connection_exists_in_atlas_via_search", lambda _: False
    )
    assert (
        client.poll_atlas_for_connection(
            "default/mysql/test",
            interval_seconds=1,
            timeout_seconds=1000,
            max_not_found_attempts_override=3,
        )
        is False
    )
