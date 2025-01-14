import logging
from abc import ABC

from application_sdk.clients.sql_client import SQLClient
from application_sdk.clients.temporal_client import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.workflows.builder import (
    MinerBuilderInterface,
    WorkflowBuilderInterface,
)
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.sql.workflows.miner import SQLMinerWorkflow
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers import TransformerInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLWorkflowBuilder(WorkflowBuilderInterface, ABC):
    sql_resource: SQLClient
    transformer: TransformerInterface
    preflight_check_controller: WorkflowPreflightCheckControllerInterface

    def set_preflight_check_controller(
        self, preflight_check_controller: WorkflowPreflightCheckControllerInterface
    ) -> "WorkflowBuilderInterface":
        self.preflight_check_controller = preflight_check_controller
        return self

    def set_sql_resource(self, sql_resource: SQLClient) -> "SQLWorkflowBuilder":
        self.sql_resource = sql_resource
        return self

    def get_sql_resource(self) -> SQLClient:
        return self.sql_resource

    def set_transformer(
        self, transformer: TransformerInterface
    ) -> "SQLWorkflowBuilder":
        self.transformer = transformer
        return self

    def build(self, workflow: SQLWorkflow | None = None) -> SQLWorkflow:
        workflow = workflow or SQLWorkflow()

        return (
            workflow.set_sql_resource(self.sql_resource)
            .set_transformer(self.transformer)
            .set_temporal_resource(self.temporal_resource)
            .set_preflight_check_controller(self.preflight_check_controller)
        )


class SQLMinerBuilder(MinerBuilderInterface, ABC):
    sql_resource: SQLClient
    transformer: TransformerInterface
    preflight_check_controller: WorkflowPreflightCheckControllerInterface

    def set_sql_resource(self, sql_resource: SQLClient) -> "SQLMinerBuilder":
        self.sql_resource = sql_resource
        return self

    def get_sql_resource(self) -> SQLClient:
        return self.sql_resource

    def set_transformer(self, transformer: TransformerInterface) -> "SQLMinerBuilder":
        self.transformer = transformer
        return self

    def set_temporal_resource(
        self, temporal_resource: TemporalClient
    ) -> "SQLMinerBuilder":
        super().set_temporal_resource(temporal_resource)
        return self

    def build(self, miner: SQLMinerWorkflow | None = None) -> SQLMinerWorkflow:
        miner = miner or SQLMinerWorkflow()

        return (
            miner.set_sql_resource(self.sql_resource)
            .set_temporal_resource(self.temporal_resource)
            .set_preflight_check_controller(self.preflight_check_controller)
        )
