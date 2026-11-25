"""Fixtures shared by the Temporal interceptor tests.

The ``workflow.info()`` / ``activity.info()`` and execute-input doubles these
tests pass in live in :mod:`tests.unit._doubles`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_next() -> AsyncMock:
    """The next interceptor in the chain: both entry points return ``"ok"``."""
    n = AsyncMock()
    n.execute_workflow = AsyncMock(return_value="ok")
    n.execute_activity = AsyncMock(return_value="ok")
    return n
