import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ConnectionInfo:
    """Standardized connection info exposed by a data source fixture after setup."""

    host: str
    port: int
    username: str
    password: str
    database: str
    extra: Dict[str, Any] = field(default_factory=dict)


class DataSourceFixture(ABC):
    """Abstract base for E2E test data source provisioning.

    Subclasses represent a specific data source (postgres, mysql, etc.)
    and a specific provisioning strategy (container vs hosted).

    Lifecycle:
        setup()            -> provisions the data source and returns ConnectionInfo
        teardown()         -> cleans up resources
        is_ready()         -> checks if the data source is accepting connections
        get_env_vars()     -> returns env vars to inject for config.yaml expansion
        wait_until_ready() -> polls is_ready() until success or timeout
    """

    @abstractmethod
    def setup(self) -> ConnectionInfo:
        """Provision the data source. Called once per test session."""
        ...

    def teardown(self) -> None:
        """Tear down the data source. Called once per test session.

        Default is a no-op. Containerized fixtures override this to stop
        the container. Hosted fixtures have nothing to tear down.
        """

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the data source is ready to accept connections."""
        ...

    @abstractmethod
    def get_env_vars(self) -> Dict[str, str]:
        """Return env vars to inject so config.yaml $VAR expansion works.

        Example return: {
            "E2E_POSTGRES_HOST": "localhost",
            "E2E_POSTGRES_PORT": "5432",
        }
        """
        ...

    def wait_until_ready(self, timeout: float = 60.0, interval: float = 2.0) -> None:
        """Poll is_ready() until the data source is up or timeout expires."""
        start = time.monotonic()
        last_error: Optional[Exception] = None
        while time.monotonic() - start < timeout:
            try:
                if self.is_ready():
                    return
            except Exception as e:
                last_error = e
            time.sleep(interval)
        raise TimeoutError(
            f"Data source not ready after {timeout}s. Last error: {last_error}"
        )
