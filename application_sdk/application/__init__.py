from abc import ABC
from typing import Optional

from application_sdk.handlers import HandlerInterface


class AtlanApplicationInterface(ABC):
    handler: Optional[HandlerInterface]

    def __init__(
        self,
        handler: Optional[HandlerInterface] = None,
    ):
        self.handler = handler

    async def start(self):
        pass

    def register_routers(self):
        pass

    def register_routes(self):
        pass
