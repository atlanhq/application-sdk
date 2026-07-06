"""Unit test configuration and autouse fixtures."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from loguru import logger as _loguru_logger

# Re-export shared registry fixtures so all unit tests can use them without
# explicit per-file imports (pytest discovers fixtures via conftest chain).
from application_sdk.testing.fixtures import (  # noqa: F401
    clean_app_registry,
    clean_task_registry,
)


@pytest.fixture
def loguru_capture():
    """Capture loguru log records emitted during the test.

    Yields a list of raw loguru ``record`` dicts (same structure as
    ``message.record`` in a loguru sink).  Extra fields bound via
    ``logger.bind(**kwargs)`` are available under ``record["extra"]``.
    """
    records: list[dict] = []
    sink_id = _loguru_logger.add(
        lambda message: records.append(message.record),
        level="DEBUG",
        format="{message}",
    )
    yield records
    _loguru_logger.remove(sink_id)


def _safe_patch(target, side_effect=None, mock_obj=None):
    """Create a patch context that gracefully handles unresolvable targets."""
    try:
        if mock_obj is not None:
            ctx = patch(target, mock_obj)
        elif side_effect is not None:
            ctx = patch(target, side_effect=side_effect)
        else:
            ctx = patch(target)
        ctx.__enter__()
        return ctx
    except (AttributeError, ModuleNotFoundError):
        return None


@pytest.fixture(autouse=True)
def _reset_dapr_sidecar_cold_start_gate(monkeypatch):
    """Reset the process-level Dapr cold-start gate before every unit test.

    ``application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready``
    is a module global shared by ``wait_for_dapr_sidecar`` and every
    ``retry_past_dapr_cold_start`` caller (agent bundle fetch, single-key
    probes, the named-credential resolver path, the GUID/vault credential
    and config-fetch paths). Without a reset, a successful resolve in one
    test would leave it set for the rest of the session, silently skipping
    the cold-start retry loop under test in a later, order-dependent test.
    """
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready", False
    )


@pytest.fixture
def fast_dapr_cold_start_retry(monkeypatch):
    """Zero out cold-start retry backoff so a retry-then-succeed test runs
    instantly instead of sleeping for real between attempts.

    Shared by every ``retry_past_dapr_cold_start`` call site's "retries a
    transient failure then succeeds" test (agent bundle fetch, single-key
    probes, the named-credential resolver path, the GUID/vault credential
    and config-fetch paths) — previously each duplicated the same three
    ``monkeypatch.setattr`` calls.
    """
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        30.0,
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_BASE_DELAY_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_DELAY_SECONDS",
        0.0,
    )


@pytest.fixture
def deterministic_dapr_cold_start_deadline(monkeypatch):
    """Deterministic fake clock + no-op sleep for a "gives up at the
    deadline" cold-start-retry test.

    Each attempt advances the fake clock by 6s against a 10s budget, so the
    retry loop gives up after exactly 2 attempts regardless of real
    wall-clock scheduling delays under load — a real-time-based loose
    ``>= 2`` assertion would flake on a contended runner. Shared by every
    ``retry_past_dapr_cold_start`` call site's deadline-exhaustion test;
    previously each duplicated the same fake-clock + mocked-sleep setup.
    """
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
        10.0,
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.asyncio.sleep", AsyncMock()
    )
    fake_now = {"t": 0.0}

    def fake_monotonic() -> float:
        fake_now["t"] += 6.0
        return fake_now["t"]

    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http.time.monotonic", fake_monotonic
    )


@pytest.fixture(autouse=True)
def mock_secret_store():
    """Automatically mock get_deployment_secret for all unit tests."""
    ctx = _safe_patch(
        "application_sdk.infrastructure.secrets.get_deployment_secret",
        side_effect=lambda key: None,
    )
    yield
    if ctx is not None:
        ctx.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def mock_dapr_client():
    """Automatically mock DaprClient for all unit tests to prevent Dapr health check timeouts."""

    def _make_mock_dapr():
        mock_instance = Mock()
        mock_instance.publish_event = Mock()
        mock_instance.invoke_binding = Mock()
        mock_instance.get_state = Mock(return_value=Mock(data=None))
        mock_instance.save_state = Mock()
        mock_instance.get_secret = Mock(return_value=Mock(secret={}))
        return mock_instance

    mock_dapr = Mock()
    mock_instance = _make_mock_dapr()
    mock_dapr.return_value.__enter__ = Mock(return_value=mock_instance)
    mock_dapr.return_value.__exit__ = Mock(return_value=None)

    ctx = _safe_patch(
        "application_sdk.infrastructure._dapr.client.DaprClient",
        mock_obj=mock_dapr,
    )
    yield
    if ctx is not None:
        ctx.__exit__(None, None, None)
