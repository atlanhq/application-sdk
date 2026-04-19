"""Shared fixtures for templates unit tests."""

from __future__ import annotations

import pytest

from application_sdk.app.registry import AppRegistry, TaskRegistry


@pytest.fixture(autouse=True)
def reset_registries() -> None:
    """Reset AppRegistry and TaskRegistry between tests."""
    AppRegistry.reset()
    TaskRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()
