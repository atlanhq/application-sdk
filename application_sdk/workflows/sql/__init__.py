import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from sqlalchemy import Engine, create_engine

from application_sdk.workflows import (
    WorkflowAuthInterface,
    WorkflowBuilderInterface,
    WorkflowMetadataInterface,
    WorkflowPreflightCheckInterface,
    WorkflowWorkerInterface,
)
from application_sdk.workflows.sql.auth import SQLWorkflowAuthInterface
from application_sdk.workflows.sql.metadata import SQLWorkflowMetadataInterface
from application_sdk.workflows.sql.preflight_check import (
    SQLWorkflowPreflightCheckInterface,
)
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

    def get_sql_engine(self, credentials: Dict[str, Any]) -> Engine:
        return create_engine(
            self.get_sqlalchemy_connection_string(credentials),
            connect_args=self.get_sqlalchemy_connect_args(credentials),
            pool_pre_ping=True,
        )

    def __init__(
        self,
        auth_interface: Optional[WorkflowAuthInterface] = None,
        metadata_interface: Optional[WorkflowMetadataInterface] = None,
        preflight_check_interface: Optional[WorkflowPreflightCheckInterface] = None,
        worker_interface: Optional[WorkflowWorkerInterface] = None,
    ):
        if not auth_interface:
            auth_interface = SQLWorkflowAuthInterface(
                self.get_sql_engine,
            )

        if not metadata_interface:
            metadata_interface = SQLWorkflowMetadataInterface(
                self.get_sql_engine,
            )

        if not preflight_check_interface:
            preflight_check_interface = SQLWorkflowPreflightCheckInterface(
                self.get_sql_engine,
            )

        if not worker_interface:
            worker_interface = SQLWorkflowWorkerInterface(
                get_sql_engine=self.get_sql_engine,
            )

        super().__init__(
            auth_interface,
            metadata_interface,
            preflight_check_interface,
            worker_interface,
        )
