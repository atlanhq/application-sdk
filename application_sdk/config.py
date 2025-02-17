import os
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ApplicationConfig(BaseModel):
    """Configuration settings for the application SDK.

    This class defines default configuration values that can be overridden either
    through environment variables or programmatically.

    To override via environment variables, use the ATLAN_ prefix:
    ATLAN_SQL_CHUNK_SIZE=50000

    To override programmatically:
    ```python
    class MyAppConfig(ApplicationConfig):
        sql_chunk_size: int = 50000
    ```

    Attributes:
        sql_chunk_size: Number of records to fetch per SQL query batch
        json_chunk_size: Number of records to process per JSON batch
        max_transform_concurrency: Maximum number of concurrent transformations
        temporal_heartbeat_timeout: Timeout for temporal heartbeats in seconds
        sql_use_server_side_cursor: Whether to use server-side cursors for SQL queries
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    # SQL Settings
    sql_chunk_size: int = Field(
        default=int(os.getenv("ATLAN_SQL_CHUNK_SIZE", "100000")),
        description="Number of records to fetch per SQL query batch",
    )
    sql_use_server_side_cursor: bool = Field(
        default=os.getenv("ATLAN_SQL_USE_SERVER_SIDE_CURSOR", "true").lower() == "true",
        description="Whether to use server-side cursors for SQL queries",
    )

    # JSON Settings
    json_chunk_size: int = Field(
        default=int(os.getenv("ATLAN_JSON_CHUNK_SIZE", "10000")),
        description="Number of records to process per JSON batch",
    )

    # Transform Settings
    max_transform_concurrency: int = Field(
        default=int(os.getenv("ATLAN_MAX_TRANSFORM_CONCURRENCY", "5")),
        description="Maximum number of concurrent transformations",
    )

    # Class variable to store the active configuration instance
    _instance: Optional["ApplicationConfig"] = None

    @classmethod
    def get_instance(cls) -> "ApplicationConfig":
        """Get the singleton instance of the configuration.

        If no instance exists, creates one with default values.

        Returns:
            ApplicationConfig: The configuration instance
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def initialize(cls, config: Optional["ApplicationConfig"] = None) -> None:
        """Initialize the configuration with custom settings.

        Args:
            config: Optional custom configuration instance
        """
        cls._instance = config or cls()


# Create default instance
config = ApplicationConfig.get_instance()
