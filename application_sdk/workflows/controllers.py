from abc import ABC, abstractmethod
from typing import Any, Dict, List

from application_sdk.inputs.statestore import StateStore
from application_sdk.logging import get_logger

logger = get_logger(__name__)


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

    def get_workflow_config(self, workflow_id: str) -> Dict[str, Any]:
        return StateStore.extract_configuration(workflow_id)

    def update_workflow_config(
        self, workflow_id: str, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        StateStore.store_configuration(workflow_id, config)
        return config


class WorkflowPreflightCheckControllerInterface(WorkflowControllerInterface, ABC):
    """
    Base class for workflow preflight check Controllers
    """

    @abstractmethod
    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
