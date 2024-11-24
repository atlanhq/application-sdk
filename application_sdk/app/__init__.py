from abc import ABC

from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)


class AtlanApplicationConfig:
    pass


class AtlanApplication(ABC):
    auth_controller: WorkflowAuthControllerInterface | None
    metadata_controller: WorkflowMetadataControllerInterface | None
    preflight_check_controller: WorkflowPreflightCheckControllerInterface | None

    def __init__(
        self,
        auth_controller: WorkflowAuthControllerInterface | None = None,
        metadata_controller: WorkflowMetadataControllerInterface | None = None,
        preflight_check_controller: WorkflowPreflightCheckControllerInterface
        | None = None,
        config: AtlanApplicationConfig = AtlanApplicationConfig(),
    ):
        self.auth_controller = auth_controller
        self.metadata_controller = metadata_controller
        self.preflight_check_controller = preflight_check_controller

        self.config = config

    async def on_server_start(self):
        pass

    async def on_server_stop(self):
        pass

    async def start(self):
        pass
