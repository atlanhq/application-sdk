from abc import ABC
from typing import Optional

from application_sdk.app import models
from application_sdk.app.database import get_engine
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)


class AtlanApplicationConfig:
    pass


class AtlanApplicationInterface(ABC):
    auth_controller: Optional[WorkflowAuthControllerInterface]
    metadata_controller: Optional[WorkflowMetadataControllerInterface]
    preflight_check_controller: Optional[WorkflowPreflightCheckControllerInterface]

    def __init__(
        self,
        auth_controller: Optional[WorkflowAuthControllerInterface] = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface] = None,
        preflight_check_controller: Optional[
            WorkflowPreflightCheckControllerInterface
        ] = None,
        config: AtlanApplicationConfig = AtlanApplicationConfig(),
    ):
        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller

        self.config = config

    async def on_app_start(self):
        models.Base.metadata.create_all(bind=get_engine())

    async def on_app_stop(self):
        models.Base.metadata.drop_all(bind=get_engine())

    async def start(self):
        pass

    def register_routers(self):
        pass

    def register_routes(self):
        pass
