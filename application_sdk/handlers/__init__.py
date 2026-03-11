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
    async def get_configmap(config_map_id: str) -> Dict[str, Any]:
        """
        Get a configmap by ID. Checks contract/generated/ first,
        then falls back to empty dict. Apps can override this for
        custom behavior.
        """
        if CONTRACT_GENERATED_DIR.exists():
            for json_file in CONTRACT_GENERATED_DIR.rglob("*.json"):
                if json_file.stem == config_map_id:
                    logger.debug(f"Serving configmap from contract: {json_file}")
                    with open(json_file) as f:
                        return json.load(f)
        return {}
