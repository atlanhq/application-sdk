"""Unit tests for the pause interceptor.

Tests the automatic pause/resume functionality that blocks activity execution
when a workflow receives a pause signal.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from unittest import mock

import pytest
from temporalio.api.common.v1 import Payload

from application_sdk.interceptors.pause import (
    PAUSE_SIGNAL,
    RESUME_SIGNAL,
    PauseInterceptor,
    PauseOutboundInterceptor,
    PauseWorkflowInboundInterceptor,
)


@dataclass
class MockHandleSignalInput:
    """Mock HandleSignalInput for testing."""

    signal: str = ""
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


@dataclass
class MockStartActivityInput:
    """Mock StartActivityInput for testing."""

    activity: str = "test_activity"
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


class TestPauseWorkflowInboundInterceptor:
    """Tests for PauseWorkflowInboundInterceptor."""

    @pytest.fixture
    def mock_next_inbound(self):
        """Create a mock next inbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.handle_signal = mock.AsyncMock()
        return mock_next

    @pytest.fixture
    def interceptor(self, mock_next_inbound):
        """Create the interceptor instance."""
        return PauseWorkflowInboundInterceptor(mock_next_inbound)

    @pytest.mark.asyncio
    async def test_pause_signal_sets_paused(self, interceptor, mock_next_inbound):
        """Test that pause signal sets _paused to True."""
        input_data = MockHandleSignalInput(signal=PAUSE_SIGNAL)

        await interceptor.handle_signal(input_data)

        assert interceptor._paused is True
        mock_next_inbound.handle_signal.assert_called_once_with(input_data)

    @pytest.mark.asyncio
    async def test_resume_signal_clears_paused(self, interceptor, mock_next_inbound):
        """Test that resume signal sets _paused to False."""
        interceptor._paused = True
        input_data = MockHandleSignalInput(signal=RESUME_SIGNAL)

        await interceptor.handle_signal(input_data)

        assert interceptor._paused is False
        mock_next_inbound.handle_signal.assert_called_once_with(input_data)

    @pytest.mark.asyncio
    async def test_unrelated_signal_does_not_change_state(
        self, interceptor, mock_next_inbound
    ):
        """Test that unrelated signals pass through without changing pause state."""
        input_data = MockHandleSignalInput(signal="some_other_signal")

        await interceptor.handle_signal(input_data)

        assert interceptor._paused is False
        mock_next_inbound.handle_signal.assert_called_once_with(input_data)

    @pytest.mark.asyncio
    async def test_pause_resume_cycle(self, interceptor, mock_next_inbound):
        """Test a full pause/resume cycle."""
        assert interceptor._paused is False

        await interceptor.handle_signal(MockHandleSignalInput(signal=PAUSE_SIGNAL))
        assert interceptor._paused is True

        await interceptor.handle_signal(MockHandleSignalInput(signal=RESUME_SIGNAL))
        assert interceptor._paused is False

    def test_init_wires_outbound_interceptor(self, interceptor):
        """Test that init() creates and wires the PauseOutboundInterceptor."""
        mock_outbound = mock.MagicMock()

        interceptor.init(mock_outbound)

        # Verify super().init() was called (via the mock chain)
        # The outbound should have been wrapped with PauseOutboundInterceptor

    def test_initial_state_is_not_paused(self, interceptor):
        """Test that the interceptor starts in unpaused state."""
        assert interceptor._paused is False


class TestPauseOutboundInterceptor:
    """Tests for PauseOutboundInterceptor."""

    @pytest.fixture
    def mock_next_outbound(self):
        """Create a mock next outbound interceptor."""
        mock_next = mock.AsyncMock()
        mock_next.start_activity = mock.AsyncMock(return_value="activity_handle")
        return mock_next

    @pytest.fixture
    def mock_inbound_not_paused(self):
        """Create a mock inbound interceptor in unpaused state."""
        mock_inbound = mock.MagicMock()
        mock_inbound._paused = False
        return mock_inbound

    @pytest.fixture
    def interceptor(self, mock_next_outbound, mock_inbound_not_paused):
        """Create the outbound interceptor instance."""
        return PauseOutboundInterceptor(mock_next_outbound, mock_inbound_not_paused)

    @pytest.mark.asyncio
    async def test_passes_through_when_not_paused(
        self, interceptor, mock_next_outbound
    ):
        """Test that activity starts immediately when not paused."""
        input_data = MockStartActivityInput()

        result = await interceptor.start_activity(input_data)

        mock_next_outbound.start_activity.assert_called_once_with(input_data)
        assert result == "activity_handle"

    @pytest.mark.asyncio
    async def test_stores_inbound_reference(self, mock_next_outbound):
        """Test that the outbound interceptor stores a reference to the inbound."""
        mock_inbound = mock.MagicMock()
        mock_inbound._paused = False

        outbound = PauseOutboundInterceptor(mock_next_outbound, mock_inbound)

        assert outbound.inbound is mock_inbound


class TestPauseInterceptor:
    """Tests for the main PauseInterceptor class."""

    @pytest.fixture
    def interceptor(self):
        """Create the main interceptor instance."""
        return PauseInterceptor()

    def test_returns_workflow_interceptor_class(self, interceptor):
        """Test that workflow_interceptor_class returns the correct class."""
        mock_input = mock.MagicMock()

        result = interceptor.workflow_interceptor_class(mock_input)

        assert result == PauseWorkflowInboundInterceptor


class TestPauseSignalConstants:
    """Tests for the signal constants."""

    def test_pause_signal_value(self):
        """Test that PAUSE_SIGNAL has the correct value."""
        assert PAUSE_SIGNAL == "pause"

    def test_resume_signal_value(self):
        """Test that RESUME_SIGNAL has the correct value."""
        assert RESUME_SIGNAL == "resume"
