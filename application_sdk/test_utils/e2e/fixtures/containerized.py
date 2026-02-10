from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from application_sdk.test_utils.e2e.fixtures.base import (
    ConnectionInfo,
    DataSourceFixture,
)


@dataclass
class VolumeMapping:
    """Describes a host-path to container-path volume mount."""

    host_path: str
    container_path: str
    mode: str = "ro"


class ContainerizedDataSourceFixture(DataSourceFixture):
    """Base for fixtures backed by a testcontainers container.

    Uses Template Method pattern for the setup lifecycle:
        create_container() -> start() -> wait_until_ready() -> extract_connection_info()

    Subclasses provide DB-specific details:
        - container_image       -- Docker image (e.g., "postgres:15.12")
        - container_env         -- env vars passed into the container
        - get_volume_mappings() -- seed file volume mounts
        - create_container()    -- build the testcontainers object
        - extract_connection_info() -- read host/port from the started container
        - is_ready()            -- DB-specific readiness probe
        - get_env_vars()        -- env vars to inject into the test process
    """

    _container: Any = None
    _connection_info: Optional[ConnectionInfo] = None

    @property
    @abstractmethod
    def container_image(self) -> str:
        """Docker image name:tag."""
        ...

    @property
    def container_env(self) -> Dict[str, str]:
        """Environment variables passed to the container. Override as needed."""
        return {}

    def get_volume_mappings(self) -> List[VolumeMapping]:
        """Return volume mappings for seed files. Override to provide seed data."""
        return []

    @abstractmethod
    def create_container(self) -> Any:
        """Create and return the testcontainers container object (not started).

        This is where the subclass uses the specific testcontainers class
        (e.g., PostgresContainer, MySqlContainer) and configures it.
        The testcontainers import should happen inside this method to keep
        it as a deferred, optional dependency.
        """
        ...

    @abstractmethod
    def extract_connection_info(self, container: Any) -> ConnectionInfo:
        """Extract ConnectionInfo from a running container.

        Called after the container is started. Read the mapped host/port
        from the container object.
        """
        ...

    def setup(self) -> ConnectionInfo:
        """Start the container, wait for readiness, return connection info."""
        self._container = self.create_container()
        self._container.start()
        self.wait_until_ready()
        self._connection_info = self.extract_connection_info(self._container)
        return self._connection_info

    def teardown(self) -> None:
        """Stop and remove the container."""
        if self._container is not None:
            self._container.stop()
            self._container = None
            self._connection_info = None
