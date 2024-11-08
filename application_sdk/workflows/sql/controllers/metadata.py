import logging
from typing import Dict, List

from application_sdk.workflows.controllers import WorkflowMetadataControllerInterface
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowMetadataController(WorkflowMetadataControllerInterface):
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

    sql_resource: SQLResource | None = None

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        """
        Fetch metadata from the database.

        :param credential: Credentials to use.
        :return: List of metadata.
        :raises Exception: If the metadata cannot be fetched.
        """
        try:
            result = []
            async for batch in self.sql_resource.run_query(self.METADATA_SQL):
                for row in batch:
                    schema_name = row["schema_name"]
                    catalog_name = row["catalog_name"]
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
