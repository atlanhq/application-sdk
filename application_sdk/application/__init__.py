from abc import ABC
from typing import Optional

from application_sdk.app import models
from application_sdk.app.database import get_engine
from application_sdk.handlers import HandlerInterface


class AtlanApplicationInterface(ABC):
    handler: Optional[HandlerInterface]

    def __init__(
        self,
        handler: Optional[HandlerInterface] = None,
    ):
        self.handler = handler

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
