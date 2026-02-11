"""Containerized data source fixture.

Uses testcontainers GenericContainer so apps don't need to write any
Python — just a datasource.yaml with image, port, env, volumes, credentials.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from application_sdk.test_utils.e2e.fixtures.base import (
    ConnectionInfo,
    DataSourceFixture,
)
from application_sdk.test_utils.e2e.fixtures.readiness import check_tcp
from application_sdk.test_utils.e2e.fixtures.schema import ContainerizedDatasourceConfig


class ContainerizedFixture(DataSourceFixture):
    """Fixture driven entirely by a ContainerizedDatasourceConfig (parsed from YAML).

    Lifecycle:
        1. Create generic DockerContainer with image, env, port, volumes
        2. Start the container
        3. Extract host:port from the running container
        4. Build ConnectionInfo from host:port + config credentials
        5. Wait until ready (TCP check by default)
        6. Return ConnectionInfo

    For custom readiness checks (e.g. verifying seed data), subclass this
    and override is_ready().
    """

    def __init__(self, config: ContainerizedDatasourceConfig, yaml_dir: Path) -> None:
        self._config = config
        self._yaml_dir = yaml_dir
        self._container: Any = None
        self._connection_info: Optional[ConnectionInfo] = None

    def setup(self) -> ConnectionInfo:
        """Start the container and return connection info."""
        from testcontainers.core.container import DockerContainer

        container = DockerContainer(self._config.image)
        container = container.with_exposed_ports(self._config.port)

        for key, value in self._config.env.items():
            container = container.with_env(key, value)

        for vol in self._config.volumes:
            host_path = str(self._resolve_volume_path(vol.host_path))
            container = container.with_volume_mapping(
                host_path, vol.container_path, vol.mode
            )

        self._container = container
        self._container.start()

        host = self._container.get_container_host_ip()
        port = int(self._container.get_exposed_port(self._config.port))

        creds = self._config.credentials
        self._connection_info = ConnectionInfo(
            host=host,
            port=port,
            username=creds.get("username", ""),
            password=creds.get("password", ""),
            database=creds.get("database", ""),
            extra={
                k: v
                for k, v in creds.items()
                if k not in ("username", "password", "database")
            },
        )

        self.wait_until_ready(
            timeout=self._config.readiness.timeout,
            interval=self._config.readiness.interval,
        )

        return self._connection_info

    def teardown(self) -> None:
        """Stop and remove the container."""
        if self._container is not None:
            self._container.stop()
            self._container = None
            self._connection_info = None

    def is_ready(self) -> bool:
        """Check if the container port is accepting TCP connections.

        Override this method for custom readiness checks (e.g. seed verification).
        """
        if self._connection_info is None:
            return False
        return check_tcp(self._connection_info.host, self._connection_info.port)

    def get_env_vars(self) -> Dict[str, str]:
        """Auto-generate env vars from env_prefix + ConnectionInfo fields.

        E.g. env_prefix="E2E_POSTGRES" produces:
            E2E_POSTGRES_HOST, E2E_POSTGRES_PORT, E2E_POSTGRES_USERNAME,
            E2E_POSTGRES_PASSWORD, E2E_POSTGRES_DATABASE
        """
        if self._connection_info is None:
            return {}

        prefix = self._config.env_prefix
        return {
            f"{prefix}_HOST": self._connection_info.host,
            f"{prefix}_PORT": str(self._connection_info.port),
            f"{prefix}_USERNAME": self._connection_info.username,
            f"{prefix}_PASSWORD": self._connection_info.password,
            f"{prefix}_DATABASE": self._connection_info.database,
        }

    def _resolve_volume_path(self, path: str) -> Path:
        """Resolve a volume host_path relative to the YAML file directory."""
        p = Path(path)
        if p.is_absolute():
            return p
        return (self._yaml_dir / p).resolve()
