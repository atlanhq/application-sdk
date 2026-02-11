import os
from unittest.mock import MagicMock, patch

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

    def _make_session(self, yaml_path=None):
        config = MagicMock(spec=[])
        if yaml_path:
            config._datasource_yaml = yaml_path
        session = MagicMock()
        session.config = config
        return session

    def test_sessionstart_noop_without_yaml(self):
        session = self._make_session()
        plugin.pytest_sessionstart(session)
        assert plugin._active_fixture is None

    def test_sessionstart_loads_yaml_and_sets_up_fixture(self, monkeypatch):
        stub = StubFixture()
        with patch(
            "application_sdk.test_utils.e2e.fixtures.loader.load_fixture_from_yaml",
            return_value=stub,
        ) as mock_load:
            session = self._make_session(yaml_path="/path/to/datasource.yaml")
            plugin.pytest_sessionstart(session)

            mock_load.assert_called_once_with("/path/to/datasource.yaml")
            assert StubFixture.setup_called
            assert plugin._active_fixture is stub
            assert os.environ.get("STUB_HOST") == "stub-host"
            assert os.environ.get("STUB_PORT") == "9999"

        monkeypatch.delenv("STUB_HOST", raising=False)
        monkeypatch.delenv("STUB_PORT", raising=False)

    def test_sessionfinish_tears_down_fixture(self, monkeypatch):
        stub = StubFixture()
        with patch(
            "application_sdk.test_utils.e2e.fixtures.loader.load_fixture_from_yaml",
            return_value=stub,
        ):
            session = self._make_session(yaml_path="/path/to/datasource.yaml")
            plugin.pytest_sessionstart(session)
            plugin.pytest_sessionfinish(session, exitstatus=0)

        assert StubFixture.teardown_called
        assert plugin._active_fixture is None

        monkeypatch.delenv("STUB_HOST", raising=False)
        monkeypatch.delenv("STUB_PORT", raising=False)

    def test_sessionfinish_noop_without_active_fixture(self):
        session = self._make_session()
        plugin.pytest_sessionfinish(session, exitstatus=0)  # should not raise

    def test_connection_info_stored_on_config(self, monkeypatch):
        stub = StubFixture()
        with patch(
            "application_sdk.test_utils.e2e.fixtures.loader.load_fixture_from_yaml",
            return_value=stub,
        ):
            session = self._make_session(yaml_path="/path/to/datasource.yaml")
            plugin.pytest_sessionstart(session)

        info = session.config._datasource_connection_info
        assert info.host == "stub-host"

        monkeypatch.delenv("STUB_HOST", raising=False)
        monkeypatch.delenv("STUB_PORT", raising=False)
