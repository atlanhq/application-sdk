"""Unit test configuration and autouse fixtures."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, Mock, patch

import pytest
from loguru import logger as _loguru_logger

from application_sdk.execution.progress import ProgressTracker, bind_progress_tracker

# Re-export shared registry fixtures so all unit tests can use them without
# explicit per-file imports (pytest discovers fixtures via conftest chain).
from application_sdk.testing.fixtures import (  # noqa: F401
    clean_app_registry,
    clean_task_registry,
)


class RecordingProgressTracker(ProgressTracker):
    """A real :class:`ProgressTracker` that also keeps every label it was given.

    ``ProgressTracker`` only retains the *most recent* label, which is all the
    stall watchdog needs but not enough to assert that a framework hook fires
    once per unit of work rather than once per record (ADR-0018's hard
    constraint). Subclassing keeps the real stall/hold behaviour intact — no
    stub — while adding the ordered label history a hook test needs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.labels: list[str] = []

    def mark_progress(self, label: str = "") -> None:
        self.labels.append(label)
        super().mark_progress(label)

    def count(self, label: str) -> int:
        """How many times *label* was recorded."""
        return self.labels.count(label)


@pytest.fixture
def progress_marks() -> Iterator[RecordingProgressTracker]:
    """Bind a recording tracker for the test, and hand it back.

    Framework hooks read the current attempt's tracker via
    :func:`~application_sdk.execution.progress.current_progress_tracker`, which
    outside an activity returns the inert tracker that discards every signal —
    so binding a real one here is what turns the hooks on for a test. Stands in
    for the per-attempt bind ``activities.py`` does in production, via the same
    ``bind_progress_tracker`` block, so a test can never leave a binding behind.
    """
    tracker = RecordingProgressTracker()
    with bind_progress_tracker(tracker):
        yield tracker


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
    """Reset the process-level Dapr cold-start gates before every unit test.

    ``application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready``
    (holistic, healthz-confirmed) and ``_dapr_component_confirmed_ready``
    (per-component, set by ``retry_past_dapr_cold_start`` callers — agent
    bundle fetch, single-key probes, the named-credential resolver path, the
    GUID/vault credential and config-fetch paths) are both module globals.
    Without a reset, a successful resolve in one test would leave them set
    for the rest of the session, silently skipping the cold-start retry loop
    under test in a later, order-dependent test.
    """
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready", False
    )
    monkeypatch.setattr(
        "application_sdk.infrastructure._dapr.http._dapr_component_confirmed_ready",
        set(),
    )


@pytest.fixture(autouse=True)
def _reset_fetched_binding_secrets_registry():
    """Reset the startup-fetched binding-secrets registry after every unit test.

    ``application_sdk.storage.binding._FETCHED_BINDING_SECRETS`` is a
    process-wide dict populated by ``_create_infrastructure`` via
    ``set_fetched_binding_secrets``.  A test that exercises the startup wiring
    with the real setter (e.g.
    ``test_main_binding_secrets.py::test_infrastructure_passes_the_fetched_secrets_to_the_resolver``)
    leaves entries behind, and a later test that expects env-only resolution
    then sees the leaked secret map and fails — order-dependent, green in
    isolation.  Same hazard class as ``_reset_dapr_sidecar_cold_start_gate``
    above; same fix shape.
    """
    yield
    from application_sdk.storage.binding import (
        _reset_fetched_binding_secrets,
    )

    _reset_fetched_binding_secrets()


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
