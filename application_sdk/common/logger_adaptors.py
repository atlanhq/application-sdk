import logging
import os
import threading
from typing import Any, MutableMapping, Tuple

from temporalio import activity, workflow


class AtlanLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter."""

        super().__init__(logger, {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        extra = {}
        extra["thread_id"] = threading.get_ident()
        extra["process_id"] = os.getpid()

        # Fetch workflow information if within the workflow context
        try:
            workflow_info = workflow.info()
            if workflow_info:
                extra["run_id"] = workflow_info.run_id
                extra["workflow_id"] = workflow_info.workflow_id
                extra["workflow_namespace"] = workflow_info.namespace
                extra["task_queue"] = workflow_info.task_queue
                extra["workflow_type"] = workflow_info.workflow_type
        except Exception:
            pass

        # Fetch activity information if within the activity context
        try:
            activity_info = activity.info()
            if activity_info:
                extra["workflow_id"] = activity_info.workflow_id
                extra["run_id"] = activity_info.workflow_run_id
                extra["activity_id"] = activity_info.activity_id
                extra["activity_type"] = activity_info.activity_type
                extra["workflow_namespace"] = activity_info.workflow_namespace
                extra["task_queue"] = activity_info.task_queue
        except Exception:
            pass

        kwargs["extra"] = extra

        return (msg, kwargs)

    def isEnabledFor(self, level: int) -> bool:
        """Override to ignore replay logs."""
        return super().isEnabledFor(level)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger
