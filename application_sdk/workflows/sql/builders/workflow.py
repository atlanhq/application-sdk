import logging
from abc import ABC

from application_sdk.workflows.builders import WorkflowBuilder
from application_sdk.workflows.controllers import WorkflowAuthController, WorkflowPreflightCheckController, \
    WorkflowMetadataController, WorkflowWorkerController
from application_sdk.workflows.resources import TemporalResource
from application_sdk.workflows.sql.controllers.auth import SQLWorkflowAuthController
from application_sdk.workflows.sql.controllers.metadata import \
    SQLWorkflowMetadataController
from application_sdk.workflows.sql.controllers.preflight_check import \
    SQLWorkflowPreflightCheckController
from application_sdk.workflows.sql.controllers.worker import SQLWorkflowWorkerController
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowBuilder(WorkflowBuilder, ABC):
    # Resources
    sql_resource: SQLResource

    # Controllers
    auth_controller: WorkflowAuthController
    preflight_check_controller: WorkflowPreflightCheckController
    metadata_controller: WorkflowMetadataController

    def __init__(
        self,

        # Resources
        sql_resource: SQLResource,
        temporal_resource: TemporalResource,

        # Controllers
        auth_controller: WorkflowAuthController,
        preflight_check_controller: WorkflowPreflightCheckController,
        metadata_controller: WorkflowMetadataController,
        worker_controller: WorkflowWorkerController,
    ):
        self.temporal_resource = temporal_resource
        self.sql_resource = sql_resource

        self.auth_controller = auth_controller or SQLWorkflowAuthController(
            sql_resource = sql_resource
        )
        self.with_metadata_controller(metadata_controller or SQLWorkflowMetadataController(
            sql_resource=sql_resource,
        ))
        self.metadata_controller = metadata_controller or SQLWorkflowMetadataController(
            sql_resource = sql_resource,
        )
        self.with_preflight_check_controller(preflight_check_controller or SQLWorkflowPreflightCheckController(
            sql_resource = sql_resource,
        ))

        super().__init__(
            worker_controller= worker_controller or SQLWorkflowWorkerController(
                sql_resource=sql_resource,
                temporal_resource=temporal_resource,
            ),
            temporal_resource=temporal_resource
        )

    def with_auth_controller(self, auth_controller):
        self.auth_controller = auth_controller
        return self

    def with_preflight_check_controller(self, preflight_check_controller):
        self.preflight_check_controller = preflight_check_controller
        return self

    def with_metadata_controller(self, metadata_controller):
        self.metadata_controller = metadata_controller
        return self

