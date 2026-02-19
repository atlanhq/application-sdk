"""CONNECTION protocol - multi-field params for SDK/driver initialization.

Covers Database connections, Message Queues, Caches (~50+ services).
Pattern: Multiple fields used to initialize SDK/driver.
"""

from typing import Any, Dict, List

from application_sdk.credentials.aliases import get_field_value
from application_sdk.credentials.protocols.base import BaseProtocol
from application_sdk.credentials.results import ApplyResult, MaterializeResult
from application_sdk.credentials.types import FieldSpec, FieldType


class ConnectionProtocol(BaseProtocol):
    """Protocol for Database connections, Message Queues, Caches.

    Pattern: Multiple fields used to initialize SDK/driver.
    This protocol doesn't apply auth to HTTP requests - it's designed
    for SDK/driver initialization.

    Configuration options (via config or config_override):
        - default_port: Default port if not specified
        - ssl_mode: Default SSL mode
        - connection_string_template: Optional template for connection strings

    Examples:
        >>> # PostgreSQL connection
        >>> protocol = ConnectionProtocol()
        >>> result = protocol.materialize({
        ...     "host": "localhost",
        ...     "port": 5432,
        ...     "database": "mydb",
        ...     "username": "user",
        ...     "password": "pass"
        ... })
        >>> result.credentials
        {'host': 'localhost', 'port': 5432, 'database': 'mydb', ...}

        >>> # Snowflake with extra fields
        >>> protocol = ConnectionProtocol()
        >>> result = protocol.materialize({
        ...     "host": "account.snowflakecomputing.com",
        ...     "username": "user",
        ...     "password": "pass",
        ...     "warehouse": "COMPUTE_WH",
        ...     "role": "ANALYST"
        ... })
    """

    default_fields: List[FieldSpec] = [
        FieldSpec(
            name="host",
            display_name="Host",
            placeholder="localhost",
            required=True,
        ),
        FieldSpec(
            name="port",
            display_name="Port",
            field_type=FieldType.NUMBER,
            required=False,
        ),
        FieldSpec(
            name="database",
            display_name="Database",
            required=False,
        ),
        FieldSpec(
            name="username",
            display_name="Username",
            required=False,
        ),
        FieldSpec(
            name="password",
            display_name="Password",
            sensitive=True,
            field_type=FieldType.PASSWORD,
            required=False,
        ),
    ]

    default_config: Dict[str, Any] = {
        "default_port": None,
        "ssl_mode": None,
        "connection_string_template": None,
    }

    def apply(
        self, credentials: Dict[str, Any], request_info: Dict[str, Any]
    ) -> ApplyResult:
        """Connection protocol doesn't apply to HTTP requests.

        This protocol is designed for SDK/driver initialization,
        not HTTP request modification.
        """
        # No HTTP modification for connection credentials
        return ApplyResult()

    def materialize(self, credentials: Dict[str, Any]) -> MaterializeResult:
        """Return connection parameters for SDK usage."""
        # Field mappings with aliases
        field_mappings = {
            "host": ["host", "hostname", "server", "endpoint"],
            "port": ["port", "port_number"],
            "database": ["database", "db", "dbname", "schema", "catalog"],
            "username": ["username", "user", "login"],
            "password": ["password", "pass", "pwd"],
        }

        connection_params: Dict[str, Any] = {}

        # Extract standard fields with alias support
        for canonical, aliases in field_mappings.items():
            value = get_field_value(credentials, canonical)
            if value is not None:
                connection_params[canonical] = value

        # Apply default port if not specified
        if "port" not in connection_params and self.config.get("default_port"):
            connection_params["port"] = self.config["default_port"]

        # Add SSL mode if configured
        if self.config.get("ssl_mode") and "ssl_mode" not in connection_params:
            connection_params["ssl_mode"] = self.config["ssl_mode"]

        # Add any extra fields from credentials (warehouse, role, account, etc.)
        extra_fields = [
            "warehouse",
            "role",
            "account",
            "schema",
            "ssl_mode",
            "sslmode",
            "connect_timeout",
            "application_name",
            "options",
        ]
        for field in extra_fields:
            if (
                field in credentials
                and credentials[field]
                and field not in connection_params
            ):
                connection_params[field] = credentials[field]

        # Also copy through any remaining fields not already processed
        for key, value in credentials.items():
            if key not in connection_params and value is not None:
                # Skip internal/metadata fields
                if not key.startswith("_") and key not in [
                    "credentialSource",
                    "secret-path",
                    "credentialGuid",
                ]:
                    connection_params[key] = value

        return MaterializeResult(credentials=connection_params)
