import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs.statestore import StateStoreInput
from application_sdk.outputs.statestore import StateStoreOutput

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


# Controller base class
class WorkflowControllerInterface(ABC):
    pass


class WorkflowAuthControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow auth Controllers
    """

    @abstractmethod
    async def prepare(self, credentials: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def test_auth(self) -> bool:
        raise NotImplementedError


class WorkflowMetadataControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow metadata Controllers
    """

    @abstractmethod
    async def prepare(self, credentials: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def fetch_metadata(self) -> List[Dict[str, str]]:
        raise NotImplementedError

    def get_workflow_config(self, config_id: str) -> Dict[str, Any]:
        return StateStoreInput.extract_configuration(config_id)

    def update_workflow_config(
        self, config_id: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        extracted_config = StateStoreInput.extract_configuration(config_id)

        for key in extracted_config.keys():
            if key in config and config[key] is not None:
                extracted_config[key] = config[key]

        StateStoreOutput.store_configuration(config_id, extracted_config)
        return extracted_config


class WorkflowPreflightCheckControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow preflight check Controllers
    """

    @abstractmethod
    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
