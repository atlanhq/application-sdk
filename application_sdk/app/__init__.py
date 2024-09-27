from abc import ABC, abstractmethod
from typing import Optional


from application_sdk.app.rest import models
from application_sdk.app.rest.database import get_engine
from application_sdk.workflows import WorkflowBuilderInterface


class AtlanApplicationBuilder(ABC):
    def __init__(
        self, workflow_builder_interface: Optional[WorkflowBuilderInterface] = None
    ):
        self.workflow_builder_interface = workflow_builder_interface

    @abstractmethod
    def add_telemetry_routes(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def add_event_routes(self) -> None:
        raise NotImplementedError

    def on_api_service_start(self):
        models.Base.metadata.create_all(bind=get_engine())

    @staticmethod
    def on_api_service_stop():
        models.Base.metadata.drop_all(bind=get_engine())

    @abstractmethod
    def add_workflows_router(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def start_worker(self) -> None:
        raise NotImplementedError

