"""Shared fixtures for execution unit tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output

# The shared gate-outcome capture, in scope for the whole directory the way a
# consumer adopts it (docs/agents/testing.md).
from application_sdk.testing import capture_preflight_outcomes  # noqa: F401


@dataclass
class SimpleExecInput(Input, allow_unbounded_fields=True):
    name: str = "World"


@dataclass
class SimpleExecOutput(Output, allow_unbounded_fields=True):
    greeting: str = ""


@pytest.fixture(autouse=True)
def reset_registries() -> None:
    """Reset AppRegistry and TaskRegistry between tests."""
    AppRegistry.reset()
    TaskRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()
