import logging
from contextvars import ContextVar
from typing import Any, MutableMapping, Tuple

from temporalio import activity, workflow

# Create a context variable for request_id
request_context: ContextVar[dict] = ContextVar("request_context", default={})


class AtlanLoggerAdapter(logging.LoggerAdapter):
    def __init__(self, logger: logging.Logger) -> None:
        """Create the logger adapter with enhanced configuration."""
        logger.setLevel(logging.INFO)

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s "
            "[workflow_id=%(workflow_id)s] "
            "[run_id=%(run_id)s] "
            "[activity_id=%(activity_id)s] "
            "[workflow_type=%(workflow_type)s] ",
            defaults={
                "workflow_id": "N/A",
                "run_id": "N/A",
                "activity_id": "N/A",
                "workflow_type": "N/A",
            },
        )

        try:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)

            logger.addHandler(console_handler)

            # Temporal workflow and activity loggers
            workflow_logger = workflow.logger
            activity_logger = activity.logger

            # Ensure they inherit from the main logger
            workflow_logger.parent = logger
            activity_logger.parent = logger

            # Set levels to match the main logger
            workflow_logger.setLevel(logging.INFO)
            activity_logger.setLevel(logging.INFO)

            # Add handlers to the loggers
            workflow_logger.addHandler(console_handler)
            activity_logger.addHandler(console_handler)

        except Exception as e:
            print(f"Failed to setup logging: {str(e)}")
            # Fallback to basic console logging
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        super().__init__(logger, {})

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> Tuple[Any, MutableMapping[str, Any]]:
        """Process the log message with temporal context."""
        if "extra" not in kwargs:
            kwargs["extra"] = {}

        # Get temporal context
        try:
            workflow_info = workflow.info()
            if workflow_info:
                kwargs["extra"].update(
                    {
                        "workflow_id": workflow_info.workflow_id,
                        "run_id": workflow_info.run_id,
                        "workflow_type": workflow_info.workflow_type,
                    }
                )
        except Exception:
            pass

        try:
            activity_info = activity.info()
            if activity_info:
                kwargs["extra"].update(
                    {
                        "workflow_id": activity_info.workflow_id,
                        "run_id": activity_info.workflow_run_id,
                        "activity_id": activity_info.activity_id,
                    }
                )
        except Exception:
            pass

        return msg, kwargs

    def isEnabledFor(self, level: int) -> bool:
        """Override to ignore replay logs."""
        return super().isEnabledFor(level)

    @property
    def base_logger(self) -> logging.Logger:
        """Underlying logger usable for actions such as adding
        handlers/formatters.
        """
        return self.logger
