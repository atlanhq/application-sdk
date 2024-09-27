import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


from application_sdk.workflows import (
    WorkflowAuthInterface,
    WorkflowBuilderInterface,
    WorkflowMetadataInterface,
    WorkflowPreflightCheckInterface,
    WorkflowWorkerInterface,
)
from application_sdk.workflows.sql.auth import SQLWorkflowAuthInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import SQLWorkflowPreflightCheckInterface
from application_sdk.workflows.sql.workflow import SQLWorkflowWorkerInterface

logger = logging.getLogger(__name__)


class SQLWorkflowBuilderInterface(WorkflowBuilderInterface, ABC):
    @abstractmethod
    def get_sqlalchemy_connection_string(self, credentials: Dict[str, Any]) -> str:
        pass

    @abstractmethod
    def get_sqlalchemy_connect_args(
        self, credentials: Dict[str, Any]
    ) -> Dict[str, Any]:
        pass

    def __init__(
        self,
        auth_interface: Optional[WorkflowAuthInterface] = None,
        metadata_interface: Optional[WorkflowMetadataInterface] = None,
        preflight_check_interface: Optional[WorkflowPreflightCheckInterface] = None,
        worker_interface: Optional[WorkflowWorkerInterface] = None,
    ):
        if not auth_interface:
            auth_interface = SQLWorkflowAuthInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        if not metadata_interface:
            metadata_interface = SQLWorkflowMetadataInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        if not preflight_check_interface:
            preflight_check_interface = SQLWorkflowPreflightCheckInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        if not worker_interface:
            worker_interface = SQLWorkflowWorkerInterface(
                self.get_sqlalchemy_connection_string,
                self.get_sqlalchemy_connect_args,
            )

        super().__init__(
            auth_interface,
            metadata_interface,
            preflight_check_interface,
            worker_interface,
        )
