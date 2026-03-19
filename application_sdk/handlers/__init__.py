# ruff: noqa: E402
import warnings

warnings.warn(
    "application_sdk.handlers is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.handler.base.Handler instead.",
    DeprecationWarning,
    stacklevel=2,
)

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# Convention: generated contract files live here
CONTRACT_GENERATED_DIR = Path.cwd() / "contract" / "generated"


class HandlerInterface(ABC):
    """
    Abstract base class for workflow handlers
    """

    @abstractmethod
    async def load(self, *args: Any, **kwargs: Any) -> None:
        """
        Method to load the handler
        """
        pass

    @abstractmethod
    async def test_auth(self, *args: Any, **kwargs: Any) -> bool:
        """
        Abstract method to test the authentication credentials
        To be implemented by the subclass
        """
        raise NotImplementedError("test_auth method not implemented")

    @abstractmethod
    async def preflight_check(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to perform preflight checks
        To be implemented by the subclass
        """
        raise NotImplementedError("preflight_check method not implemented")

    @abstractmethod
    async def fetch_metadata(self, *args: Any, **kwargs: Any) -> Any:
        """
        Abstract method to fetch metadata
        To be implemented by the subclass
        """
        raise NotImplementedError("fetch_metadata method not implemented")

    @staticmethod
    def _wrap_configmap(config_map_id: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Wrap raw config JSON in the K8s ConfigMap shape expected by the frontend.

        Args:
            config_map_id: The name for the ConfigMap metadata.
            raw: The raw config dict loaded from a template file. If it has a
                 top-level "config" key, that value is used; otherwise the whole
                 dict is serialized.

        Returns:
            Dict in K8s ConfigMap format: {kind, apiVersion, metadata, data.config}
        """
        return {
            "kind": "ConfigMap",
            "apiVersion": "v1",
            "metadata": {"name": config_map_id},
            "data": {"config": json.dumps(raw.get("config", raw))},
        }

    @staticmethod
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        """Return the K8s ConfigMap for the given config_map_id.

        Priority:
        1. contract/generated/{config_map_id}.json (if exists)
        2. Subclass override (call _wrap_configmap to produce correct shape)
        3. Empty dict
        """
        if CONTRACT_GENERATED_DIR.exists():
            for json_file in CONTRACT_GENERATED_DIR.rglob("*.json"):
                if json_file.stem == config_map_id:
                    logger.debug(f"Serving configmap from contract: {json_file}")
                    with open(json_file) as f:
                        raw = json.load(f)
                    return HandlerInterface._wrap_configmap(config_map_id, raw)
        return {}
