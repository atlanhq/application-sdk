import json
from abc import ABC, abstractmethod
from pathlib import Path
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
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        """
        Get the configmap for a given config_map_id.

        Searches for a matching JSON file in contract/generated/ by filename.
        JSON files are named by their configmap ID (e.g. atlan-redshift.json).
        """
        contract_dir = Path.cwd() / "contract" / "generated"
        if not contract_dir.exists():
            return {}

        for json_file in contract_dir.rglob("*.json"):
            if json_file.stem == config_map_id:
                with open(json_file) as f:
                    return json.load(f)

        return {}

    @staticmethod
    async def get_uiconfig() -> Dict[str, Any]:
        """
        Static method to get the UI config
        """
        return {}
