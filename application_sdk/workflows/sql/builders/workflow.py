import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy import Connection, Engine, text
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.types import CallableType

from application_sdk.paas.readers.json import JSONChunkedObjectStoreReader
from application_sdk.paas.secretstore import SecretStore
from application_sdk.paas.writers.json import JSONChunkedObjectStoreWriter
from application_sdk.workflows import WorkflowWorkerInterface
from application_sdk.workflows.sql.utils import prepare_filters
from application_sdk.workflows.transformers import TransformerInterface
from application_sdk.workflows.utils.activity import auto_heartbeater

logger = logging.getLogger(__name__)


class SQLWorkflowBuilderInterface(WorkflowBuilderInterface, ABC):
    # Resources
    sql_resource: SQLResource
    identity_resource: IdentityResource

    # Interfaces
    auth_interface: AuthInterface
    preflight_check_interface: PreflightCheckInterface
    metadata_interface: MetadataInterface
    worker_interface: WorkerInterface

    def __init__(
            self,

    ):
        super().__init__()

    def with_auth(self, auth_interface):
        self.auth_interface = auth_interface
        return self

    def with_worker(self, worker_interface):
        self.worker_interface = worker_interface
        return self

    def with_preflight(self, preflight_check_interface):
        self.preflight_check_interface = preflight_check_interface
        return self

    def with_metadata(self, metadata_interface):
        self.metadata_interface = metadata_interface
        return self

    def build(self):
        # Resources
        if not self.sql_resource:
            self.sql_resource = SQLResource()

        if not self.identity_resource:
            self.identity_resource = BasicCredentialsIdentityResource()

        # Interfaces
        if not self.auth_interface:
            self.auth_interface = SQLWorkflowAuthInterface()
        self.auth_interface.with_sql_resource(sql_resource)

        if not self.metadata_interface:
            self.metadata_interface = SQLWorkflowMetadataInterface()

        if not self.preflight_check_interface:
            self.preflight_check_interface = SQLWorkflowPreflightCheckInterface()

        if not self.worker_interface:
            self.worker_interface = SQLWorkflowWorkerInterface()

        super().build()
