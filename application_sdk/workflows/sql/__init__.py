import logging
from abc import ABC
from typing import Optional

from application_sdk.workflows import (
    WorkflowAuthControllerInterface,
    WorkflowBuilderInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
    WorkflowWorkerControllerInterface,
)
from application_sdk.workflows.resources import TemporalResource
from application_sdk.workflows.sql.controllers.auth import SQLWorkflowAuthController
from application_sdk.workflows.sql.controllers.metadata import (
    SQLWorkflowMetadataController,
)
from application_sdk.workflows.sql.controllers.preflight_check import (
    SQLWorkflowPreflightCheckController,
)
from application_sdk.workflows.sql.controllers.worker import SQLWorkflowWorkerController
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowBuilderInterface(WorkflowBuilderInterface, ABC):
    def __init__(
        self,
        # Resources
        sql_resource: SQLResource,
        temporal_resource: TemporalResource,
        # Controllers
        auth_controller: Optional[WorkflowAuthControllerInterface] = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface] = None,
        preflight_check_controller: Optional[
            WorkflowPreflightCheckControllerInterface
        ] = None,
        worker_controller: Optional[WorkflowWorkerControllerInterface] = None,
    ):
        self.sql_resource = sql_resource

        if not auth_controller:
            auth_controller = SQLWorkflowAuthController(
                sql_resource=sql_resource,
            )

        if not metadata_controller:
            metadata_controller = SQLWorkflowMetadataController(
                sql_resource=sql_resource,
            )

        if not preflight_check_controller:
            preflight_check_controller = SQLWorkflowPreflightCheckController(
                sql_resource=sql_resource,
            )

        if not worker_controller:
            worker_controller = SQLWorkflowWorkerController(
                sql_resource=sql_resource,
            )

        super().__init__(
            temporal_resource=temporal_resource,
            auth_controller=auth_controller,
            metadata_controller=metadata_controller,
            preflight_check_controller=preflight_check_controller,
            worker_controller=worker_controller,
        )

    async def load_resources(self):
        await self.sql_resource.load()

        await super().load_resources()
