import logging
from abc import ABC
from typing import Optional

from application_sdk.workflows.builders import WorkflowBuilderInterface
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
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


class SQLWorkflowBuilder(WorkflowBuilderInterface, ABC):
    # Resources
    sql_resource: SQLResource

    # Controllers
    auth_controller: WorkflowAuthControllerInterface
    preflight_check_controller: WorkflowPreflightCheckControllerInterface
    metadata_controller: WorkflowMetadataControllerInterface
    worker_controller: SQLWorkflowWorkerController

    def __init__(
        self,
        # Resources
        sql_resource: SQLResource,
        temporal_resource: TemporalResource,
        # Controllers
        auth_controller: Optional[WorkflowAuthControllerInterface] = None,
        preflight_check_controller: Optional[
            WorkflowPreflightCheckControllerInterface
        ] = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface] = None,
        worker_controller: Optional[WorkflowWorkerControllerInterface] = None,
    ):
        worker_controller = worker_controller or SQLWorkflowWorkerController(
            sql_resource=sql_resource,
            temporal_resource=temporal_resource,
        )

        self.temporal_resource = temporal_resource
        self.sql_resource = sql_resource

        self.with_auth_controller(
            auth_controller
            or SQLWorkflowAuthController(
                sql_resource=self.sql_resource,
            )
        )
        self.with_metadata_controller(
            metadata_controller
            or SQLWorkflowMetadataController(
                sql_resource=sql_resource,
            )
        )
        self.with_preflight_check_controller(
            preflight_check_controller
            or SQLWorkflowPreflightCheckController(
                sql_resource=sql_resource,
            )
        )
        self.with_worker_controller(worker_controller)

        super().__init__(
            worker_controller=worker_controller, temporal_resource=temporal_resource
        )

    async def load_resources(self):
        await self.sql_resource.load()

        await super().load_resources()

    def with_auth_controller(self, auth_controller):
        self.auth_controller = auth_controller
        return self

    def with_preflight_check_controller(self, preflight_check_controller):
        self.preflight_check_controller = preflight_check_controller
        return self

    def with_metadata_controller(self, metadata_controller):
        self.metadata_controller = metadata_controller
        return self

    def with_worker_controller(self, worker_controller):
        self.worker_controller = worker_controller
