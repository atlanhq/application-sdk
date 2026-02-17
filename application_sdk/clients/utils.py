from application_sdk.clients.workflow import WorkflowEngineType
from application_sdk.constants import APPLICATION_NAME


def get_workflow_client(
    engine_type: WorkflowEngineType = WorkflowEngineType.TEMPORAL,
    application_name: str = APPLICATION_NAME,
):
    """
    Get a workflow client based on the engine type.

    Args:
        engine_type: The type of workflow engine to use
        application_name: The name of the application

    Returns:
        A workflow client instance
    """
    if engine_type == WorkflowEngineType.TEMPORAL:
        # Lazy import: TemporalWorkflowClient pulls in temporalio + grpc
        # which are not needed at module load time
        from application_sdk.clients.temporal import TemporalWorkflowClient

        return TemporalWorkflowClient(application_name=application_name)
    else:
        raise ValueError(f"Unsupported workflow engine type: {engine_type}")
