import os
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class ApplicationConfig(BaseSettings):
    """Configuration settings for the application SDK.

    This class defines default configuration values that can be overridden either
    through environment variables or programmatically.

    To override via environment variables, use the ATLAN_ prefix:
    ATLAN_SQL_CHUNK_SIZE=50000

    Attributes:
        sql_chunk_size: Number of records to fetch per SQL query batch
        json_chunk_size: Number of records to process per JSON batch
        max_transform_concurrency: Maximum number of concurrent transformations
        sql_use_server_side_cursor: Whether to use server-side cursors for SQL queries
    """

    # SQL Settings
    sql_chunk_size: int = 100000
    sql_use_server_side_cursor: bool = True

    # JSON Settings
    json_chunk_size: int = 10000

    # Transform Settings
    max_transform_concurrency: int = 5

    model_config = SettingsConfigDict(
        env_prefix="ATLAN_",
        case_sensitive=False,
        extra="allow",
        arbitrary_types_allowed=True,
    )


@lru_cache
def get_settings() -> ApplicationConfig:
    """Get cached settings instance.
    
    Returns:
        ApplicationConfig: Cached settings instance
    """
    return ApplicationConfig()


# Create default instance
settings = get_settings()
