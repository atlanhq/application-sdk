from abc import ABC
from typing import Optional

from application_sdk.app import models
from application_sdk.app.database import get_engine
from application_sdk.handlers import WorkflowHandlerInterface


class AtlanApplicationConfig:
    pass


class AtlanApplication(ABC):
    """
    Base class for Atlan applications.
    """

    handler: Optional[WorkflowHandlerInterface]

    def __init__(
        self,
        handler: Optional[WorkflowHandlerInterface] = None,
        config: AtlanApplicationConfig = AtlanApplicationConfig(),
    ):
        self.handler = handler
        self.config = config

    async def on_app_start(self):
        models.Base.metadata.create_all(bind=get_engine())

    async def on_app_stop(self):
        models.Base.metadata.drop_all(bind=get_engine())

    async def start(self):
        pass
