import logging
from typing import Any, Callable, Dict, List

from sqlalchemy import Engine, text

from application_sdk.workflows import WorkflowMetadataInterface

logger = logging.getLogger(__name__)


class SQLWorkflowMetadataInterface(WorkflowMetadataInterface):
    """
    SQL Workflow Metadata Interface

    This interface is used to fetch metadata from the database.

    Attributes:
        METADATA_SQL (str): The SQL query to fetch the metadata.
        DATABASE_KEY (str): The key to fetch the database name.
        SCHEMA_KEY (str): The key to fetch the schema name.

    Usage:
        Subclass this interface and implement the required attributes and any methods
        that need custom behavior (ex. fetch_metadata).

        >>> class MySQLWorkflowMetadataInterface(SQLWorkflowMetadataInterface):
        >>>     METADATA_SQL = "SELECT * FROM information_schema.schemata"
        >>>     DATABASE_KEY = "TABLE_CATALOG"
        >>>     SCHEMA_KEY = "TABLE_SCHEMA"
        >>>     def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        >>>         super().__init__(create_engine_fn)
    """

    METADATA_SQL: str = ""
    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        self.create_engine_fn = create_engine_fn

    # FIXME: duplicate with SQLWorkflowPreflightCheckInterface
    def fetch_metadata(self, credential: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        Fetch metadata from the database.

        :param credential: Credentials to use.
        :return: List of metadata.
        :raises Exception: If the metadata cannot be fetched.
        """
        try:
            engine = self.create_engine_fn(credential)
            with engine.connect() as connection:
                cursor = connection.execute(text(self.METADATA_SQL))
                result: List[Dict[str, str]] = []
                while True:
                    rows = cursor.fetchmany(1000)
                    if not rows:
                        break
                    for schema_name, catalog_name in rows:
                        result.append(
                            {
                                self.DATABASE_KEY: catalog_name,
                                self.SCHEMA_KEY: schema_name,
                            }
                        )
        except Exception as e:
            logger.error(f"Failed to fetch metadata: {str(e)}")
            raise e
        return result
