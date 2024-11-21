from abc import ABC, abstractmethod

from uvicorn import Config, Server
from uvicorn._types import ASGIApplication

from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)


class AtlanApplicationConfig:
    host: str = "0.0.0.0"
    port: int = 8000


# TODO: The name of this class is not great, it is specific to HTTP.
class AtlanApplication(ABC):
    auth_controller: WorkflowAuthControllerInterface | None
    metadata_controller: WorkflowMetadataControllerInterface | None
    preflight_check_controller: WorkflowPreflightCheckControllerInterface | None

    app: ASGIApplication

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

        self.register_routes()
        self.register_routers()

    @abstractmethod
    def register_routers(self):
        pass

    @abstractmethod
    def register_routes(self):
        pass

    async def start(self):
        server = Server(
            Config(
                app=self.app,
                host=self.config.host,
                port=self.config.port,
            )
        )
        await server.serve()
