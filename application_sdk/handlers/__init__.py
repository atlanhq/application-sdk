from abc import ABC, abstractmethod
from typing import Any, Dict

from application_sdk.inputs.statestore import StateStore


class HandlerInterface(ABC):
    """
    Abstract base class for workflow handlers
    """

    # TODO: Need for prepare? Or should we call it load?
    @abstractmethod
    async def prepare(self, **kwargs: Any) -> None:
        """
        Method to prepare the handler
        """
        pass

    @abstractmethod
    async def test_auth(self, **kwargs: Any) -> bool:
        """
        Abstract method to test the authentication credentials
        To be implemented by the subclass
        """
        raise NotImplementedError("test_auth method not implemented")

    @abstractmethod
    async def preflight_check(self, **kwargs: Any) -> Any:
        """
        Abstract method to perform preflight checks
        To be implemented by the subclass
        """
        raise NotImplementedError("preflight_check method not implemented")

    @abstractmethod
    async def fetch_metadata(self, **kwargs: Any) -> Any:
        """
        Abstract method to fetch metadata
        To be implemented by the subclass
        """
        raise NotImplementedError("fetch_metadata method not implemented")

    def get_workflow_config(self, config_id: str) -> Dict[str, Any]:
        return StateStore.extract_configuration(config_id)

    def update_workflow_config(
        self, config_id: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        extracted_config = StateStore.extract_configuration(config_id)

        for key in extracted_config.keys():
            if key in config and config[key] is not None:
                extracted_config[key] = config[key]

        StateStore.store_configuration(config_id, extracted_config)
        return extracted_config
