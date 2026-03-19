# ruff: noqa: E402
import warnings

warnings.warn(
    "application_sdk.workflows.query_extraction is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.templates.SqlQueryExtractor instead.",
    DeprecationWarning,
    stacklevel=2,
)

from application_sdk.workflows import WorkflowInterface


class QueryExtractionWorkflow(WorkflowInterface):
    pass
