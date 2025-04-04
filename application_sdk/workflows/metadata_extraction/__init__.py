from application_sdk.activities.metadata_extraction.sql import (
    SQLMetadataExtractionActivities,
)
from application_sdk.workflows import WorkflowInterface


class MetadataExtractionWorkflow(WorkflowInterface[SQLMetadataExtractionActivities]):
    activities_cls = SQLMetadataExtractionActivities
