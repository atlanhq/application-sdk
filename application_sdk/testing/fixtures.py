"""Pytest fixtures for testing Apps.

Import these fixtures in your conftest.py or test files::

    from application_sdk.testing.fixtures import app_context, mock_state_store

Or import everything via the testing module::

    from application_sdk.testing import app_context, mock_state_store

For an HTTP source with no container to pull, ``http_fake_source_factory`` fills
the testcontainer slot. Build the fake in a session-scoped fixture and return
its base URL; recordings are reset automatically before every test::

    @pytest.fixture(scope="session")
    def source_url(http_fake_source_factory) -> str:
        fake = http_fake_source_factory(name="my-source")
        fake.route(r"/api/v1/objects", list_objects)
        return fake.base_url

The autouse reset only applies where these fixtures are visible, so a conftest
must import ``reset_http_fake_sources`` alongside ``http_fake_source_factory``.
"""

from collections.abc import Generator

import pytest

from application_sdk.app.context import AppContext
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.constants import LOCAL_WORKFLOW_ID
from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter
from application_sdk.testing.fake_source import HttpFakeSourceFactory
from application_sdk.testing.mocks import (
    MockBinding,
    MockCredentialStore,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)

_ACTIVE_FACTORY: HttpFakeSourceFactory | None = None


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


@pytest.fixture(scope="session")
def http_fake_source_factory() -> Generator[HttpFakeSourceFactory, None, None]:
    """Session-scoped factory for started HttpFakeSource servers.

    The slot a testcontainer fixture occupies, for an HTTP source with no image to
    pull. Call it from the connector's own session-scoped fixture to build a fake,
    register that connector's routes on the returned instance, and return its
    ``base_url``; every fake built here is stopped when the session ends::

        @pytest.fixture(scope="session")
        def source_url(http_fake_source_factory) -> str:
            fake = http_fake_source_factory(name="my-source")
            fake.route(r"/api/v1/objects", list_objects)
            return fake.base_url

    Once this fixture is live, ``reset_http_fake_sources`` clears every fake's
    recorded requests and route hits before each test, so per-test assertions
    never read a previous test's traffic.
    """
    global _ACTIVE_FACTORY
    factory = HttpFakeSourceFactory()
    _ACTIVE_FACTORY = factory
    try:
        yield factory
    finally:
        _ACTIVE_FACTORY = None
        factory.stop_all()


@pytest.fixture(autouse=True)
def reset_http_fake_sources(request: pytest.FixtureRequest) -> None:
    """Reset every session fake before each test, once the factory exists.

    Autouse and function-scoped, so the pairing between the session factory and
    the per-test reset is structural rather than something every test remembers
    to request. Tests running before the factory is first instantiated have no
    fakes to reset and pay nothing.
    """
    del request
    if _ACTIVE_FACTORY is not None:
        _ACTIVE_FACTORY.reset_all()


@pytest.fixture
def clean_http_fake_sources(
    http_fake_source_factory: HttpFakeSourceFactory,
) -> Generator[HttpFakeSourceFactory, None, None]:
    """The session factory, with recordings reset before and after the test.

    The before-test reset already happens automatically via
    ``reset_http_fake_sources``; this fixture exists for suites that want to
    request the factory explicitly, and it additionally resets after the test.
    """
    http_fake_source_factory.reset_all()
    yield http_fake_source_factory
    http_fake_source_factory.reset_all()
