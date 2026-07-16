"""Unit tests for the LivenessInterceptor."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.execution._temporal.interceptors.liveness import (
    LivenessInterceptor,
    _LivenessActivityInboundInterceptor,
    _LivenessActivityOutboundInterceptor,
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

    inbound = _LivenessActivityInboundInterceptor(next_inbound, lambda: calls.append(1))
    result = await inbound.execute_activity(_MockExecuteActivityInput())

    assert result == "done"
    assert calls == [1]
    next_inbound.execute_activity.assert_awaited_once()


def test_records_on_heartbeat() -> None:
    calls: list[int] = []
    next_outbound = MagicMock()

    outbound = _LivenessActivityOutboundInterceptor(
        next_outbound, lambda: calls.append(1)
    )
    outbound.heartbeat("progress", 42)

    assert calls == [1]
    next_outbound.heartbeat.assert_called_once_with("progress", 42)


def test_init_wraps_outbound_so_heartbeats_are_recorded() -> None:
    calls: list[int] = []
    next_inbound = MagicMock()

    inbound = _LivenessActivityInboundInterceptor(next_inbound, lambda: calls.append(1))
    inbound.init(MagicMock())

    # The outbound passed downstream must be our recording wrapper.
    (passed_outbound,), _ = next_inbound.init.call_args
    assert isinstance(passed_outbound, _LivenessActivityOutboundInterceptor)
    passed_outbound.heartbeat()
    assert calls == [1]


def test_intercept_activity_returns_recording_inbound() -> None:
    interceptor = LivenessInterceptor(lambda: None)
    wrapped = interceptor.intercept_activity(MagicMock())
    assert isinstance(wrapped, _LivenessActivityInboundInterceptor)
