import os
from unittest.mock import MagicMock

from application_sdk.test_utils.e2e.fixtures import plugin
from application_sdk.test_utils.e2e.fixtures.base import (
    ConnectionInfo,
    DataSourceFixture,
)


class StubFixture(DataSourceFixture):
    """Stub fixture for plugin tests."""

    setup_called = False
    teardown_called = False

    def setup(self) -> ConnectionInfo:
        StubFixture.setup_called = True
        return ConnectionInfo("stub-host", 9999, "u", "p", "db")

    def teardown(self) -> None:
        StubFixture.teardown_called = True

    def is_ready(self) -> bool:
        return True

    def get_env_vars(self) -> dict:
        return {"STUB_HOST": "stub-host", "STUB_PORT": "9999"}


class TestPlugin:
    def setup_method(self):
        StubFixture.setup_called = False
        StubFixture.teardown_called = False
        plugin._active_fixture = None

    def _make_session(self, fixture_class=None):
        config = MagicMock()
        if fixture_class:
            config._datasource_fixture_class = fixture_class
        else:
            del config._datasource_fixture_class
        session = MagicMock()
        session.config = config
        return session

    def test_sessionstart_noop_without_fixture_class(self):
        session = self._make_session(fixture_class=None)
        plugin.pytest_sessionstart(session)
        assert plugin._active_fixture is None

    def test_sessionstart_creates_and_sets_up_fixture(self, monkeypatch):
        session = self._make_session(fixture_class=StubFixture)
        plugin.pytest_sessionstart(session)

        assert StubFixture.setup_called
        assert plugin._active_fixture is not None
        assert os.environ.get("STUB_HOST") == "stub-host"
        assert os.environ.get("STUB_PORT") == "9999"

        # Cleanup
        monkeypatch.delenv("STUB_HOST", raising=False)
        monkeypatch.delenv("STUB_PORT", raising=False)

    def test_sessionfinish_tears_down_fixture(self):
        session = self._make_session(fixture_class=StubFixture)
        plugin.pytest_sessionstart(session)
        plugin.pytest_sessionfinish(session, exitstatus=0)

        assert StubFixture.teardown_called
        assert plugin._active_fixture is None

    def test_sessionfinish_noop_without_active_fixture(self):
        session = self._make_session()
        plugin.pytest_sessionfinish(session, exitstatus=0)  # should not raise

    def test_connection_info_stored_on_config(self, monkeypatch):
        session = self._make_session(fixture_class=StubFixture)
        plugin.pytest_sessionstart(session)

        session.config._datasource_connection_info = ConnectionInfo(
            "stub-host", 9999, "u", "p", "db"
        )
        info = session.config._datasource_connection_info
        assert info.host == "stub-host"

        monkeypatch.delenv("STUB_HOST", raising=False)
        monkeypatch.delenv("STUB_PORT", raising=False)
