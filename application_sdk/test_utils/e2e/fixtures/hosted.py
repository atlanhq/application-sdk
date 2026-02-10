import os
from typing import Dict

from application_sdk.test_utils.e2e.fixtures.base import (
    ConnectionInfo,
    DataSourceFixture,
)


class HostedDataSourceFixture(DataSourceFixture):
    """Fixture for non-containerizable data sources (Snowflake, Redshift, etc.).

    Reads credentials from pre-existing environment variables.
    No provisioning or teardown needed.

    Subclasses override get_required_env_vars() to declare which env vars
    map to which ConnectionInfo fields.
    """

    def get_required_env_vars(self) -> Dict[str, str]:
        """Return mapping of logical name to env var name.

        Example: {"host": "SNOWFLAKE_HOST", "username": "SNOWFLAKE_USER", ...}
        Override in subclass.
        """
        return {}

    def setup(self) -> ConnectionInfo:
        """Read credentials from environment. Validate all required vars exist."""
        required = self.get_required_env_vars()
        missing = [
            env_var for env_var in required.values() if not os.environ.get(env_var)
        ]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        values = {key: os.environ[env_var] for key, env_var in required.items()}

        return ConnectionInfo(
            host=values.get("host", ""),
            port=int(values.get("port", "0")),
            username=values.get("username", ""),
            password=values.get("password", ""),
            database=values.get("database", ""),
            extra={
                k: v
                for k, v in values.items()
                if k not in ("host", "port", "username", "password", "database")
            },
        )

    def is_ready(self) -> bool:
        """Hosted sources are assumed ready if env vars are present."""
        return True

    def get_env_vars(self) -> Dict[str, str]:
        """No injection needed — env vars already exist."""
        return {}
