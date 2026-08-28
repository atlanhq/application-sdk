"""Pytest fixtures for testing Apps.

Import these fixtures in your conftest.py or test files::

    from application_sdk.testing.fixtures import app_context, mock_state_store

Or import everything via the testing module::

    from application_sdk.testing import app_context, mock_state_store

These are the cross-tier fixtures — registry isolation, mocked infrastructure,
logger-state restoration. The integration tier's own fixtures, including
``http_fake_source_factory`` for an HTTP source with no container to pull, live
in :mod:`application_sdk.testing.integration.fixtures` and are star-imported
from a connector's ``tests/integration/conftest.py`` as one unit.
"""

from collections.abc import Generator

import pytest

from application_sdk.app.context import AppContext
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.constants import LOCAL_WORKFLOW_ID
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter
from application_sdk.testing.mocks import (
    MockBinding,
    MockCredentialStore,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)


@pytest.fixture
def mock_state_store() -> MockStateStore:
    """Fresh MockStateStore instance."""
    return MockStateStore()


@pytest.fixture
def mock_secret_store() -> MockSecretStore:
    """Fresh MockSecretStore instance."""
    return MockSecretStore()


@pytest.fixture
def mock_pubsub() -> MockPubSub:
    """Fresh MockPubSub instance."""
    return MockPubSub()


@pytest.fixture
def mock_binding() -> MockBinding:
    """Fresh MockBinding instance."""
    return MockBinding()


@pytest.fixture
def mock_heartbeat() -> MockHeartbeatController:
    """Fresh MockHeartbeatController instance."""
    return MockHeartbeatController()


@pytest.fixture
def mock_credential_store() -> MockCredentialStore:
    """Fresh MockCredentialStore instance."""
    return MockCredentialStore()


@pytest.fixture
def app_context(
    mock_state_store: MockStateStore,
    mock_secret_store: MockSecretStore,
) -> AppContext:
    """AppContext wired with MockStateStore and MockSecretStore."""
    return AppContext(
        app_name="test-app",
        app_version="0.0.1",
        workflow_id=LOCAL_WORKFLOW_ID,
        _state_store=mock_state_store,
        _secret_store=mock_secret_store,
    )


@pytest.fixture
def clean_app_registry() -> Generator[AppRegistry, None, None]:
    """AppRegistry reset before and after each test."""
    AppRegistry.reset()
    registry = AppRegistry.get_instance()
    yield registry
    AppRegistry.reset()


@pytest.fixture
def clean_task_registry() -> Generator[TaskRegistry, None, None]:
    """TaskRegistry reset before and after each test."""
    TaskRegistry.reset()
    registry = TaskRegistry.get_instance()
    yield registry
    TaskRegistry.reset()


@pytest.fixture(autouse=True)
def restore_logger_init_flags() -> Generator[None, None, None]:
    """Stop a test that reset the logger's init state from leaking it forward.

    Autouse, so importing it into a conftest is all a suite has to do.

    :meth:`AtlanLoggerAdapter._reset_for_testing` sets ``_initialized`` False so
    a test can drive a fresh init. That flag is what gates a *destructive* step:
    ``AtlanLoggerAdapter.__init__`` calls loguru's ``logger.remove()`` — every
    sink in the process, not just its own — before registering its handlers.
    Most callers of the reset then construct an adapter, which flips the flag
    back. One that resets and instead *mocks* the adapter's ``logger`` never
    does, and exits with the flag still down.

    The next test to run then pays for it. Its first lazy ``get_logger()`` — the
    metrics/segment import chain fires one inside ``storage.upload_file``, among
    others — re-initialises and takes out whatever sinks that test had installed,
    including a log-capture fixture's. The symptom lands in *teardown*, as a
    ``ValueError: There is no existing handler with id N`` from a
    ``logger.remove(sink_id)`` for a sink that is already gone, attributed to a
    test that did nothing wrong.

    Both flags are treated as **latches: this fixture only ever restores one
    upward, and never writes False.** Writing a snapshot back verbatim would
    re-arm the very hazard it exists to close — a test that *starts* with the
    flag down and then constructs an adapter has installed live sinks by the
    time it ends, and stamping False back over that hands the next uncached
    ``get_logger()`` a licence to remove them. Only the True → False transition
    is a leak; False → True is a test doing the initialisation properly, and it
    must be allowed to stand.

    Restoring upward is exact rather than approximate. ``_reset_for_testing``
    removes no sink itself, so a test that reset without re-initialising left the
    original handlers in place and True is the truth. And there is no such thing
    as a legitimately-uninitialised process to protect: importing this module
    imports ``logger_adaptor``, whose import binds ``default_logger`` and so has
    already run a full init.

    ``_flush_task_started`` gets the same treatment for the same reason in
    reverse — it gates thread spawning, so restoring it *down* after a test
    started a flush task would let a later init spawn a second one.

    Under ``-n auto --dist load`` xdist hands items out as workers free up, so
    which pair collides is a function of relative test speed rather than of
    collection order. That is why this surfaced as a single red matrix leg that a
    rerun could turn green — the same shape as FND-961. See FND-976.
    """
    initialized = AtlanLoggerAdapter._initialized
    flush_task_started = AtlanLoggerAdapter._flush_task_started
    try:
        yield
    finally:
        if initialized:
            AtlanLoggerAdapter._initialized = True
        if flush_task_started:
            AtlanLoggerAdapter._flush_task_started = True
