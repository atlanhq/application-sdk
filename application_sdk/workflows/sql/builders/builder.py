import logging
from abc import ABC

from application_sdk.clients.sql_client import SQLClient
from application_sdk.clients.temporal_client import TemporalClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers import HandlerInterface
from application_sdk.workflows.builder import (
    MinerBuilderInterface,
    WorkflowBuilderInterface,
)
from application_sdk.workflows.sql.workflows.miner import SQLMinerWorkflow
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers import TransformerInterface

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLWorkflowBuilder(WorkflowBuilderInterface, ABC):
    sql_client: SQLClient
    transformer: TransformerInterface
    handler: HandlerInterface

    def set_handler(self, handler: HandlerInterface) -> "WorkflowBuilderInterface":
        self.handler = handler
        return self

    def set_sql_client(self, sql_client: SQLClient) -> "SQLWorkflowBuilder":
        self.sql_client = sql_client
        return self

    def get_sql_client(self) -> SQLClient:
        return self.sql_client

    def set_transformer(
        self, transformer: TransformerInterface
    ) -> "SQLWorkflowBuilder":
        self.transformer = transformer
        return self

    def build(self, workflow: SQLWorkflow | None = None) -> SQLWorkflow:
        workflow = workflow or SQLWorkflow()

        return (
            workflow.set_sql_client(self.sql_client)
            .set_transformer(self.transformer)
            .set_handler(self.handler)
            .set_temporal_client(self.temporal_client)
        )


class SQLMinerBuilder(MinerBuilderInterface, ABC):
    sql_client: SQLClient
    transformer: TransformerInterface
    handler: HandlerInterface

    def set_sql_client(self, sql_client: SQLClient) -> "SQLMinerBuilder":
        self.sql_client = sql_client
        return self

    def get_sql_client(self) -> SQLClient:
        return self.sql_client

    def set_transformer(self, transformer: TransformerInterface) -> "SQLMinerBuilder":
        self.transformer = transformer
        return self

    def set_temporal_client(self, temporal_client: TemporalClient) -> "SQLMinerBuilder":
        super().set_temporal_client(temporal_client)
        return self

    def build(self, miner: SQLMinerWorkflow | None = None) -> SQLMinerWorkflow:
        miner = miner or SQLMinerWorkflow()

        return (
            miner.set_sql_client(self.sql_client)
            .set_temporal_client(self.temporal_client)
            .set_handler(self.handler)
        )
