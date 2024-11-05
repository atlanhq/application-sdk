from abc import ABC, abstractmethod
from typing import Optional

from application_sdk.app import models
from application_sdk.app.database import get_engine
from application_sdk.workflows.builders import WorkflowBuilderInterface


class AtlanApplicationBuilder(ABC):
    def __init__(
        self, workflow_builder_interface: Optional[WorkflowBuilderInterface] = None
    ):
        self.workflow_builder_interface = workflow_builder_interface

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
