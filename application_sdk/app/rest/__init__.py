from abc import ABC, abstractmethod
from typing import Optional

from uvicorn import Config, Server
from uvicorn._types import ASGIApplication

from application_sdk.app import AtlanApplication, AtlanApplicationConfig
from application_sdk.handlers import HandlerInterface


class AtlanAPIApplicationConfig(AtlanApplicationConfig):
    host: str = "0.0.0.0"
    port: int = 8000

    def __init__(self, host: str = "0.0.0.0", port: int = 8000, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.host = host
        self.port = port


class AtlanAPIApplication(AtlanApplication, ABC):
    app: ASGIApplication
    config: AtlanAPIApplicationConfig

    def __init__(
        self,
        handler: Optional[HandlerInterface] = None,
        config: AtlanAPIApplicationConfig = AtlanAPIApplicationConfig(),
    ):
        super().__init__(
            handler=handler,
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
