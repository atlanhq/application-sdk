"""Worker event models.

This module contains event models specific to worker operations.
"""

from typing import Optional

from pydantic import BaseModel


class WorkerCreationEventData(BaseModel):
    """Model for worker creation event data.

    This model represents the data structure used when publishing worker creation events.
    It contains information about the worker configuration and environment.

    Attributes:
        application_name: Name of the application the worker belongs to.
        task_queue: Task queue name for the worker.
        namespace: Temporal namespace for the worker.
        host: Host address of the Temporal server.
        port: Port number of the Temporal server.
        connection_string: Connection string for the Temporal server.
        max_concurrent_activities: Maximum number of concurrent activities.
        workflow_count: Number of workflow classes registered.
        activity_count: Number of activity functions registered.
    """

    version: str = "v1"
    application_name: str
    task_queue: str
    namespace: str
    host: str
    port: str
    connection_string: str
    max_concurrent_activities: Optional[int]
    workflow_count: int
    activity_count: int
