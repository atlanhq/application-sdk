from typing import Any

from application_sdk.clients.temporal import TemporalWorkflowClient
from application_sdk.clients.workflow import WorkflowEngineType
from application_sdk.constants import APPLICATION_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def extract_column_name(description_item: Any) -> str:
    """Extract column name from a cursor description item.

    DB-API 2.0 (PEP 249) specifies that cursor.description returns a sequence of
    7-item sequences: (name, type_code, display_size, internal_size, precision, scale, null_ok).

    However, different database drivers implement this differently:
    - Some return named tuples with a `.name` attribute (e.g., psycopg2, cx_Oracle)
    - Some return plain tuples where name is at index 0 (e.g., clickhouse-connect)
    - SQLAlchemy's CursorResult wraps these and may provide `.name` attribute

    This function handles both formats to ensure compatibility across all drivers.

    Args:
        description_item: A single item from cursor.description, which can be either:
            - A named tuple/object with a `.name` attribute
            - A plain tuple/sequence where name is at index 0

    Returns:
        str: The column name in lowercase.

    Example:
        >>> # Named tuple format (psycopg2, cx_Oracle)
        >>> extract_column_name(Column(name='ID', type_code=1))
        'id'

        >>> # Plain tuple format (clickhouse-connect)
        >>> extract_column_name(('ID', 'UInt64', None, None, None, None, True))
        'id'
    """
    # Try .name attribute first (SQLAlchemy wrapped cursors, psycopg2, cx_Oracle, etc.)
    if hasattr(description_item, "name"):
        return str(description_item.name).lower()

    # Fall back to index 0 for plain tuples (clickhouse-connect, some ODBC drivers)
    # PEP 249 specifies name is always the first element
    if isinstance(description_item, (tuple, list)) and len(description_item) > 0:
        return str(description_item[0]).lower()

    # Last resort: convert to string
    logger.warning(
        f"Unexpected cursor description format: {type(description_item)}. "
        "Falling back to string conversion."
    )
    return str(description_item).lower()


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
        return TemporalWorkflowClient(application_name=application_name)
    else:
        raise ValueError(f"Unsupported workflow engine type: {engine_type}")
