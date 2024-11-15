from abc import ABC, abstractmethod
from typing import Optional

from application_sdk.app import models
from application_sdk.app.database import get_engine
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)


class AtlanApplicationBuilder(ABC):
    auth_controller: WorkflowAuthControllerInterface | None
    metadata_controller: WorkflowMetadataControllerInterface | None
    preflight_check_controller: WorkflowPreflightCheckControllerInterface | None

    def __init__(
        self,
        auth_controller: Optional[WorkflowAuthControllerInterface] | None = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface]
        | None = None,
        preflight_check_controller: Optional[WorkflowPreflightCheckControllerInterface]
        | None = None,
    ):
        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller

    @abstractmethod
    def add_telemetry_routes(self) -> None:
        raise NotImplementedError

    def on_api_service_start(self):
        models.Base.metadata.create_all(bind=get_engine())

    @staticmethod
    def on_api_service_stop():
        models.Base.metadata.drop_all(bind=get_engine())

    @abstractmethod
    def add_workflows_router(self) -> None:
        raise NotImplementedError
