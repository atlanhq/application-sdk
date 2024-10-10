from typing import Tuple, MutableMapping, Any
import logging
from temporalio import activity, workflow

class OpenTelemetryLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    def __init__(
        self, logger: logging.Logger
    ) -> None:
        """Create the logger adapter."""
        super().__init__(logger, {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        extra = {}

        # Fetch workflow information if within the workflow context
        try:
            workflow_info = workflow.info()
            if workflow_info:
                extra["run_id"] = workflow_info.run_id
                extra["workflow_id"] = workflow_info.workflow_id
        except Exception:
            pass

        # Fetch activity information if within the workflow context
        try:
            activity_info = activity.info()
            if activity_info:
                extra["workflow_id"] = activity_info.workflow_id
                extra["run_id"] = activity_info.workflow_run_id
                extra["activity_id"] = activity_info.activity_id
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

