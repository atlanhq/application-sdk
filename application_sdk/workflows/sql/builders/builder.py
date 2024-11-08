import logging
from abc import ABC

from application_sdk.workflows.builder import WorkflowBuilderInterface
from application_sdk.workflows.resources import TemporalResource
from application_sdk.workflows.sql.resources.sql_resource import SQLResource
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow
from application_sdk.workflows.transformers import TransformerInterface

logger = logging.getLogger(__name__)


class SQLWorkflowBuilder(WorkflowBuilderInterface, ABC):
    sql_resource: SQLResource
    temporal_resource: TemporalResource
    transformer: TransformerInterface

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

    def set_temporal_resource(
        self, temporal_resource: TemporalResource
    ) -> "SQLWorkflowBuilder":
        self.temporal_resource = temporal_resource
        return self

    def build(self, workflow: SQLWorkflow | None = None) -> SQLWorkflow:
        workflow = workflow or SQLWorkflow()

        return (
            workflow.set_sql_resource(self.sql_resource)
            .set_transformer(self.transformer)
            .set_temporal_resource(self.temporal_resource)
        )
