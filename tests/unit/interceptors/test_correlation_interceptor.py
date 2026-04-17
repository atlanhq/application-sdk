"""Regression tests for the v3 CorrelationContextInterceptor.

Specifically covers the legacy header fallback (BLDX-900 fix): the AE's older
interceptor injects correlation_id under the plain header key "correlation_id"
(no x- prefix).  The new interceptor must read that as a fallback so that
AE-dispatched workflows inherit the correct correlation_id without any AE changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import default as default_converter

from application_sdk.execution._temporal.interceptors.correlation_interceptor import (
    _HEADER_CORRELATION_ID,
    _HEADER_CORRELATION_ID_LEGACY,
    _CorrelationActivityInboundInterceptor,
    _CorrelationWorkflowInboundInterceptor,
)
from application_sdk.observability.correlation import (
    CorrelationContext,
    get_correlation_context,
    set_correlation_context,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode(value: str) -> Payload:
    return default_converter().payload_converter.to_payload(value)


@dataclass
class _WorkflowInput:
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


@dataclass
class _ActivityInput:
    args: Sequence[Any] = field(default_factory=list)
    headers: Mapping[str, Payload] = field(default_factory=dict)


def _make_workflow_interceptor() -> _CorrelationWorkflowInboundInterceptor:
    mock_next = MagicMock()
    mock_next.execute_workflow = AsyncMock(return_value="result")
    return _CorrelationWorkflowInboundInterceptor(mock_next)


def _make_activity_interceptor() -> _CorrelationActivityInboundInterceptor:
    mock_next = MagicMock()
    mock_next.execute_activity = AsyncMock(return_value="result")
    return _CorrelationActivityInboundInterceptor(mock_next)


# ---------------------------------------------------------------------------
# Workflow inbound — header fallback
# ---------------------------------------------------------------------------


class TestWorkflowInboundLegacyHeader:
    """Workflow interceptor reads correlation_id from the legacy header key."""

    def setup_method(self):
        set_correlation_context(CorrelationContext(correlation_id=""))

    async def test_new_header_key_works(self):
        """Primary path: x-correlation-id header is read correctly."""
        interceptor = _make_workflow_interceptor()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("temporalio.workflow.memo", lambda: {})
            await interceptor.execute_workflow(
                _WorkflowInput(
                    headers={_HEADER_CORRELATION_ID: _encode("new-corr-123")}
                )
            )

        assert interceptor._correlation_id == "new-corr-123"
        ctx = get_correlation_context()
        assert ctx is not None and ctx.correlation_id == "new-corr-123"

    async def test_legacy_header_key_fallback(self):
        """BLDX-900 regression: plain 'correlation_id' header (AE style) is read."""
        interceptor = _make_workflow_interceptor()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("temporalio.workflow.memo", lambda: {})
            await interceptor.execute_workflow(
                _WorkflowInput(
                    headers={_HEADER_CORRELATION_ID_LEGACY: _encode("ae-corr-456")}
                )
            )

        assert interceptor._correlation_id == "ae-corr-456"
        ctx = get_correlation_context()
        assert ctx is not None and ctx.correlation_id == "ae-corr-456"

    async def test_new_header_takes_precedence_over_legacy(self):
        """If both headers are present, x-correlation-id wins."""
        interceptor = _make_workflow_interceptor()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("temporalio.workflow.memo", lambda: {})
            await interceptor.execute_workflow(
                _WorkflowInput(
                    headers={
                        _HEADER_CORRELATION_ID: _encode("new-wins"),
                        _HEADER_CORRELATION_ID_LEGACY: _encode("legacy-loses"),
                    }
                )
            )

        assert interceptor._correlation_id == "new-wins"

    async def test_no_header_generates_uuid(self):
        """Priority 3: no headers → a fresh UUID is generated."""
        interceptor = _make_workflow_interceptor()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("temporalio.workflow.memo", lambda: {})
            await interceptor.execute_workflow(_WorkflowInput(headers={}))

        assert interceptor._correlation_id != ""
        assert len(interceptor._correlation_id) == 36  # UUID4 format


# ---------------------------------------------------------------------------
# Activity inbound — header fallback
# ---------------------------------------------------------------------------


class TestActivityInboundLegacyHeader:
    """Activity interceptor reads correlation_id from the legacy header key."""

    def setup_method(self):
        set_correlation_context(CorrelationContext(correlation_id=""))

    async def test_new_header_key_works(self):
        interceptor = _make_activity_interceptor()
        await interceptor.execute_activity(
            _ActivityInput(headers={_HEADER_CORRELATION_ID: _encode("act-new-789")})
        )

        ctx = get_correlation_context()
        assert ctx is not None and ctx.correlation_id == "act-new-789"

    async def test_legacy_header_key_fallback(self):
        """Activity inherits AE correlation_id from the legacy header."""
        interceptor = _make_activity_interceptor()
        await interceptor.execute_activity(
            _ActivityInput(
                headers={_HEADER_CORRELATION_ID_LEGACY: _encode("ae-act-corr-abc")}
            )
        )

        ctx = get_correlation_context()
        assert ctx is not None and ctx.correlation_id == "ae-act-corr-abc"

    async def test_new_header_takes_precedence_in_activity(self):
        interceptor = _make_activity_interceptor()
        await interceptor.execute_activity(
            _ActivityInput(
                headers={
                    _HEADER_CORRELATION_ID: _encode("act-new-wins"),
                    _HEADER_CORRELATION_ID_LEGACY: _encode("act-legacy-loses"),
                }
            )
        )

        ctx = get_correlation_context()
        assert ctx is not None and ctx.correlation_id == "act-new-wins"

    async def test_no_header_leaves_context_unchanged(self):
        """Activity with no correlation header does not wipe existing context."""
        set_correlation_context(CorrelationContext(correlation_id="pre-existing"))
        interceptor = _make_activity_interceptor()
        await interceptor.execute_activity(_ActivityInput(headers={}))

        ctx = get_correlation_context()
        assert ctx is not None and ctx.correlation_id == "pre-existing"
