"""Minimal deployment configuration manager."""

from typing import Any, Dict, Optional

from application_sdk.constants import (
    DEPLOYMENT_SECRET_PATH,
    DEPLOYMENT_SECRET_STORE_NAME,
)
from application_sdk.inputs.secretstore import SecretStoreInput
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class DeploymentConfig:
    """Simple deployment config manager with caching."""

    _config: Optional[Dict[str, Any]] = None

    @classmethod
    async def get(cls, force_refresh: bool = False) -> Dict[str, Any]:
        """Get deployment config with caching."""
        if cls._config is None or force_refresh:
            try:
                cls._config = await SecretStoreInput.get_secret(
                    DEPLOYMENT_SECRET_PATH, DEPLOYMENT_SECRET_STORE_NAME
                )
            except Exception as e:
                logger.error(f"Failed to fetch deployment config: {e}")
                cls._config = {}

        return cls._config

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached config."""
        cls._config = None
