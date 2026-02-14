from unittest.mock import patch

import pytest

from application_sdk.test_utils.e2e.fixtures.base import (
    ConnectionInfo,
    DataSourceFixture,
)


class ConcreteFixture(DataSourceFixture):
    """Minimal concrete implementation for testing the ABC."""

    def __init__(self, ready: bool = True):
        self._ready = ready
        self._setup_called = False
        self._teardown_called = False

    def setup(self) -> ConnectionInfo:
        self._setup_called = True
        return ConnectionInfo(
            host="localhost",
            port=5432,
            credentials={"username": "user", "password": "pass", "database": "testdb"},
        )

    def teardown(self) -> None:
        self._teardown_called = True

    def is_ready(self) -> bool:
        return self._ready

    def get_env_vars(self) -> dict:
        return {"TEST_HOST": "localhost", "TEST_PORT": "5432"}


class TestConnectionInfo:
    def test_creation_with_defaults(self):
        info = ConnectionInfo(host="localhost", port=5432)
        assert info.host == "localhost"
        assert info.port == 5432
        assert info.credentials == {}

    def test_creation_with_credentials(self):
        info = ConnectionInfo(
            host="localhost",
            port=5432,
            credentials={
                "username": "user",
                "password": "pass",
                "database": "testdb",
                "api_token": "tok123",
            },
        )
        assert info.credentials["username"] == "user"
        assert info.credentials["api_token"] == "tok123"


class TestDataSourceFixture:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            DataSourceFixture()  # type: ignore[abstract]

    def test_concrete_fixture_lifecycle(self):
        fixture = ConcreteFixture()
        info = fixture.setup()
        assert info.host == "localhost"
        assert fixture._setup_called

        fixture.teardown()
        assert fixture._teardown_called

    def test_get_env_vars(self):
        fixture = ConcreteFixture()
        env_vars = fixture.get_env_vars()
        assert env_vars == {"TEST_HOST": "localhost", "TEST_PORT": "5432"}

    def test_wait_until_ready_succeeds(self):
        fixture = ConcreteFixture(ready=True)
        fixture.wait_until_ready(timeout=1.0, interval=0.1)

    def test_wait_until_ready_times_out(self):
        fixture = ConcreteFixture(ready=False)
        with pytest.raises(TimeoutError, match="Data source not ready after"):
            fixture.wait_until_ready(timeout=0.3, interval=0.1)

    def test_wait_until_ready_captures_last_error(self):
        class ErrorFixture(ConcreteFixture):
            def is_ready(self) -> bool:
                raise ConnectionError("connection refused")

        fixture = ErrorFixture()
        with pytest.raises(TimeoutError, match="connection refused"):
            fixture.wait_until_ready(timeout=0.3, interval=0.1)

    @patch("time.sleep")
    def test_wait_until_ready_retries(self, mock_sleep):
        call_count = 0

        class EventuallyReadyFixture(ConcreteFixture):
            def is_ready(self) -> bool:
                nonlocal call_count
                call_count += 1
                return call_count >= 3

        fixture = EventuallyReadyFixture()
        fixture.wait_until_ready(timeout=10.0, interval=0.1)
        assert call_count >= 3
