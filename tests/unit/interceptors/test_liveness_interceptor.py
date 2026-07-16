"""Unit tests for the LivenessInterceptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.execution._temporal.interceptors.liveness import (
    LivenessInterceptor,
)


@dataclass
class _MockExecuteActivityInput:
    headers: dict = field(default_factory=dict)
    args: list = field(default_factory=list)


@pytest.mark.asyncio
async def test_records_on_activity_execution() -> None:
    calls: list[int] = []
    next_inbound = MagicMock()
    next_inbound.execute_activity = AsyncMock(return_value="done")

    inbound = LivenessInterceptor(lambda: calls.append(1)).intercept_activity(
        next_inbound
    )
    result = await inbound.execute_activity(_MockExecuteActivityInput())

    assert result == "done"
    assert calls == [1]
    next_inbound.execute_activity.assert_awaited_once()


def test_records_on_heartbeat() -> None:
    calls: list[int] = []
    next_inbound = MagicMock()

    # Drive the public path: intercept_activity() -> inbound; init() installs the
    # recording outbound wrapper downstream, which we then exercise via heartbeat.
    inbound = LivenessInterceptor(lambda: calls.append(1)).intercept_activity(
        next_inbound
    )
    inbound.init(MagicMock())
    (passed_outbound,), _ = next_inbound.init.call_args

    passed_outbound.heartbeat("progress", 42)

    assert calls == [1]
    passed_outbound.next.heartbeat.assert_called_once_with("progress", 42)


def test_init_wraps_outbound_so_heartbeats_are_recorded() -> None:
    calls: list[int] = []
    next_inbound = MagicMock()

    inbound = LivenessInterceptor(lambda: calls.append(1)).intercept_activity(
        next_inbound
    )
    inbound.init(MagicMock())

    # The outbound passed downstream must record on heartbeat.
    (passed_outbound,), _ = next_inbound.init.call_args
    passed_outbound.heartbeat()
    assert calls == [1]


@pytest.mark.asyncio
async def test_record_callback_failure_never_blocks_activity() -> None:
    """A raising record callback must not fail the activity (best-effort guard)."""
    next_inbound = MagicMock()
    next_inbound.execute_activity = AsyncMock(return_value="done")

    def _boom() -> None:
        raise RuntimeError("record blew up")

    inbound = LivenessInterceptor(_boom).intercept_activity(next_inbound)
    result = await inbound.execute_activity(_MockExecuteActivityInput())

    assert result == "done"
    next_inbound.execute_activity.assert_awaited_once()
