import logging
from abc import ABC

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.workflows.builder import (
    MinerBuilderInterface,
    WorkflowBuilderInterface,
)
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.resources.temporal_resource import TemporalResource
from application_sdk.workflows.sql.resources.sql_resource import SQLResource
from application_sdk.workflows.sql.workflows.miner import SQLMinerWorkflow
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers import TransformerInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLWorkflowBuilder(WorkflowBuilderInterface, ABC):
    sql_resource: SQLResource
    transformer: TransformerInterface
    preflight_check_controller: WorkflowPreflightCheckControllerInterface

    def set_preflight_check_controller(
        self, preflight_check_controller: WorkflowPreflightCheckControllerInterface
    ) -> "WorkflowBuilderInterface":
        self.preflight_check_controller = preflight_check_controller
        return self

    def set_sql_resource(self, sql_resource: SQLResource) -> "SQLWorkflowBuilder":
        self.sql_resource = sql_resource
        return self

    def get_sql_resource(self) -> SQLResource:
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
    sql_resource: SQLResource
    transformer: TransformerInterface

    def set_sql_resource(self, sql_resource: SQLResource) -> "SQLMinerBuilder":
        self.sql_resource = sql_resource
        return self

    def get_sql_resource(self) -> SQLResource:
        return self.sql_resource

    def set_transformer(self, transformer: TransformerInterface) -> "SQLMinerBuilder":
        self.transformer = transformer
        return self

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
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
