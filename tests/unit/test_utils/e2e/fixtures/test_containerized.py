from unittest.mock import MagicMock

import pytest

from application_sdk.test_utils.e2e.fixtures.base import ConnectionInfo
from application_sdk.test_utils.e2e.fixtures.containerized import (
    ContainerizedDataSourceFixture,
    VolumeMapping,
)


class FakeContainerFixture(ContainerizedDataSourceFixture):
    """Generic concrete implementation using a mock container for testing."""

    def __init__(self):
        self._mock_container = MagicMock()
        self._mock_container.get_container_host_ip.return_value = "127.0.0.1"
        self._mock_container.get_exposed_port.return_value = 15000

    @property
    def container_image(self) -> str:
        return "datasource:latest"

    @property
    def container_env(self) -> dict:
        return {"DB_USER": "test", "DB_PASSWORD": "test"}

    def get_volume_mappings(self) -> list:
        return [
            VolumeMapping(
                host_path="/tmp/seed.sql",
                container_path="/docker-entrypoint-initdb.d/seed.sql",
            )
        ]

    def create_container(self):
        return self._mock_container

    def extract_connection_info(self, container) -> ConnectionInfo:
        return ConnectionInfo(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5000)),
            username="test",
            password="test",
            database="testdb",
        )

    def is_ready(self) -> bool:
        return True

    def get_env_vars(self) -> dict:
        if self._connection_info is None:
            raise RuntimeError("Not set up yet")
        return {
            "E2E_HOST": self._connection_info.host,
            "E2E_PORT": str(self._connection_info.port),
        }


class TestVolumeMapping:
    def test_defaults(self):
        vm = VolumeMapping(host_path="/a", container_path="/b")
        assert vm.mode == "ro"

    def test_custom_mode(self):
        vm = VolumeMapping(host_path="/a", container_path="/b", mode="rw")
        assert vm.mode == "rw"


class TestContainerizedDataSourceFixture:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            ContainerizedDataSourceFixture()  # type: ignore[abstract]

    def test_setup_starts_container_and_extracts_info(self):
        fixture = FakeContainerFixture()
        info = fixture.setup()

        fixture._mock_container.start.assert_called_once()
        assert info.host == "127.0.0.1"
        assert info.port == 15000
        assert info.username == "test"

    def test_teardown_stops_container(self):
        fixture = FakeContainerFixture()
        fixture.setup()
        fixture.teardown()

        fixture._mock_container.stop.assert_called_once()
        assert fixture._container is None
        assert fixture._connection_info is None

    def test_teardown_noop_without_setup(self):
        fixture = FakeContainerFixture()
        fixture.teardown()  # should not raise

    def test_get_env_vars_after_setup(self):
        fixture = FakeContainerFixture()
        fixture.setup()
        env_vars = fixture.get_env_vars()
        assert env_vars["E2E_HOST"] == "127.0.0.1"
        assert env_vars["E2E_PORT"] == "15000"

    def test_get_env_vars_before_setup_raises(self):
        fixture = FakeContainerFixture()
        with pytest.raises(RuntimeError, match="Not set up yet"):
            fixture.get_env_vars()

    def test_default_container_env_is_empty(self):
        class MinimalFixture(ContainerizedDataSourceFixture):
            @property
            def container_image(self) -> str:
                return "test:latest"

            def create_container(self):
                return MagicMock()

            def extract_connection_info(self, container) -> ConnectionInfo:
                return ConnectionInfo("h", 0, "u", "p", "d")

            def is_ready(self) -> bool:
                return True

            def get_env_vars(self) -> dict:
                return {}

        fixture = MinimalFixture()
        assert fixture.container_env == {}
        assert fixture.get_volume_mappings() == []
