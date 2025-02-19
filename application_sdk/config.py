"""Configuration settings for application-sdk using Pydantic."""
from typing import Dict, Any
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ApplicationSDKSettings(BaseSettings):
    """Central configuration for application-sdk.
    
    This class uses Pydantic's BaseSettings which allows for configuration via environment
    variables and/or direct assignment. Environment variables take precedence over defaults.
    
    Environment Variables:
        ATLAN_APP_SDK_SQL_CHUNK_SIZE: Chunk size for SQL query operations
        ATLAN_APP_SDK_JSON_CHUNK_SIZE: Chunk size for JSON operations
        ATLAN_APP_SDK_MAX_TRANSFORM_CONCURRENCY: Maximum concurrent transformations
        ATLAN_APP_SDK_PARQUET_CHUNK_SIZE: Chunk size for Parquet operations
        ATLAN_APP_SDK_ICEBERG_CHUNK_SIZE: Chunk size for Iceberg operations
    """
    
    model_config = SettingsConfigDict(
        env_prefix="ATLAN_APP_SDK_",
        case_sensitive=False,
        # Allow extra fields for future extensibility
        extra="allow",
    )
    
    # SQL Input/Output settings
    sql_chunk_size: int = 100000
    
    # JSON Input/Output settings
    json_chunk_size: int = 100000
    
    # Parquet Output settings
    parquet_chunk_size: int = 30000
    
    # Iceberg Input settings
    iceberg_chunk_size: int = 100000
    
    # Workflow settings
    max_transform_concurrency: int = 5
    
    def model_dump(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """Get settings as a dictionary."""
        return super().model_dump(*args, **kwargs)
    
    @classmethod
    def get_settings(cls, **kwargs: Any) -> "ApplicationSDKSettings":
        """Create settings with optional overrides."""
        return cls(**kwargs)


# Global settings instance with default values
settings = ApplicationSDKSettings()


@lru_cache()
def get_settings() -> ApplicationSDKSettings:
    """Get the global settings instance.
    
    Returns:
        ApplicationSDKSettings: The global settings instance.
    
    Note:
        This function is cached to avoid re-reading environment variables.
        To refresh settings, call get_settings.cache_clear()
    """
    return settings


def configure_settings(**kwargs: Any) -> None:
    """Configure global settings with overrides.
    
    Args:
        **kwargs: Keyword arguments to override default settings.
    
    Example:
        >>> configure_settings(sql_chunk_size=50000, max_transform_concurrency=10)
    """
    global settings
    settings = ApplicationSDKSettings.get_settings(**kwargs)
    # Clear the cache to ensure get_settings returns the new values
    get_settings.cache_clear() 