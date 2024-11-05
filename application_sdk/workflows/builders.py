import asyncio
import logging
import os
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Sequence

from dependency_injector import containers
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.types import CallableType, ClassType
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxRestrictions,
)

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.logging import get_logger
from application_sdk.workflows.controllers import WorkflowWorkerController
from application_sdk.workflows.resources import TemporalResource

logger = get_logger(__name__)

# TODO: Rename it to WorkflowBuilderInterface
# Same with other files
class WorkflowBuilder(ABC):
    """
    Base class for workflow builder interfaces

    This class provides a default implementation for the workflow builder, with hooks
    for subclasses to customize specific behaviors.

    Attributes:
        worker_interface: The worker interface.
    """

    worker_controller: WorkflowWorkerController

    def __init__(
        self,

        # Resources
        temporal_resource: TemporalResource,

        # Interfaces
        worker_controller: WorkflowWorkerController,
    ):
        worker_controller = worker_controller or WorkflowWorkerController(
            temporal_resource
        )
        self.with_worker_controller(worker_controller)

    def with_worker_controller(self, worker_controller):
        self.worker_controller = worker_controller
        return self

    def start_worker(self):
        if not self.worker_controller:
            raise NotImplementedError("Worker controller not implemented")
        asyncio.run(self.worker_controller.start_worker())
