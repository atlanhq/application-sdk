from abc import ABC, abstractmethod
from typing import Optional

from uvicorn import Config, Server
from uvicorn._types import ASGIApplication

from application_sdk.application import (
    AtlanApplicationConfig,
    AtlanApplicationInterface,
)
from application_sdk.workflows.controllers import (
    WorkflowAuthControllerInterface,
    WorkflowMetadataControllerInterface,
    WorkflowPreflightCheckControllerInterface,
)


class AtlanAPIApplicationConfig(AtlanApplicationConfig):
    host: str = "0.0.0.0"
    port: int = 8000

    def __init__(self, host: str = "0.0.0.0", port: int = 8000, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.host = host
        self.port = port


class AtlanAPIApplication(AtlanApplicationInterface, ABC):
    app: ASGIApplication
    config: AtlanAPIApplicationConfig

    def __init__(
        self,
        auth_controller: Optional[WorkflowAuthControllerInterface] = None,
        metadata_controller: Optional[WorkflowMetadataControllerInterface] = None,
        preflight_check_controller: Optional[
            WorkflowPreflightCheckControllerInterface
        ] = None,
        config: AtlanAPIApplicationConfig = AtlanAPIApplicationConfig(),
    ):
        super().__init__(
            auth_controller=auth_controller,
            metadata_controller=metadata_controller,
            preflight_check_controller=preflight_check_controller,
            config=config,
        )

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
