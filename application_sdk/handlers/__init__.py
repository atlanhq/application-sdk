import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict

import yaml


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
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        """
        Get the configmap for a given config_map_id.

        Default implementation searches pkl-generated YAML configmaps in
        contract/generated/ and matches by metadata.name. Apps can override
        this to read from custom locations.
        """
        contract_dir = Path.cwd() / "contract" / "generated"
        if not contract_dir.exists():
            return {}

        for yaml_file in contract_dir.rglob("*.yaml"):
            with open(yaml_file) as f:
                doc = yaml.safe_load(f)
            if not isinstance(doc, dict):
                continue
            if doc.get("metadata", {}).get("name") == config_map_id:
                config_str = doc.get("data", {}).get("config", "{}")
                return {"config": json.loads(config_str)}

        return {}
