import logging
from abc import ABC
from typing import Optional

from application_sdk.workflows.builder import (
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
    sql_resource: SQLResource

    auth_controller: SQLWorkflowAuthController
    preflight_check_controller: SQLWorkflowPreflightCheckController
    metadata_controller: SQLWorkflowMetadataController

    def set_auth_controller(self, auth_controller: SQLWorkflowAuthController):
        self.auth_controller = auth_controller

    def set_preflight_check_controller(
        self, preflight_check_controller: SQLWorkflowPreflightCheckController
    ):
        self.preflight_check_controller = preflight_check_controller

    def set_sql_resource(self, sql_resource: SQLResource):
        self.sql_resource = sql_resource

    def get_sql_resource(self) -> SQLResource:
        return self.sql_resource
