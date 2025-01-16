from temporalio import activity


def get_workflow_id() -> str:
    return activity.info().workflow_id
