from enum import Enum


class LogEventType(Enum):
    # Workflow Events
    WORKFLOW_START = "workflow_start"
    WORKFLOW_END = "workflow_end"
    WORKFLOW_ERROR = "workflow_error"

    # Activity Events
    ACTIVITY_START = "activity_start"
    ACTIVITY_END = "activity_end"
    ACTIVITY_ERROR = "activity_error"

    # State Events
    STATE_CHANGE = "state_change"
    STATE_ERROR = "state_error"

    # Data Processing Events
    DATA_PROCESSING_START = "data_processing_start"
    DATA_PROCESSING_END = "data_processing_end"
    DATA_PROCESSING_ERROR = "data_processing_error"

    # Connection Events
    CONNECTION_START = "connection_start"
    CONNECTION_END = "connection_end"
    CONNECTION_ERROR = "connection_error"
