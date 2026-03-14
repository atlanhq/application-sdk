import json
from abc import ABC, abstractmethod
from typing import Any, Dict


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

        Override in subclass to load app-specific template JSON, then call
        HandlerInterface._wrap_configmap(config_map_id, raw) to produce the
        correct shape.
        """
        return {}
