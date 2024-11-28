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

    DATABASE_ALIAS_KEY: str | None = None
    SCHEMA_ALIAS_KEY: str | None = None

    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    sql_resource: SQLResource | None = None

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    async def fetch_metadata(self, credential: Dict[str, str]) -> List[Dict[str, str]]:
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")

        self.sql_resource.set_credentials(credential)
        await self.sql_resource.load()
        args = {
            "metadata_sql": self.METADATA_SQL,
            "database_alias_key": self.DATABASE_ALIAS_KEY,
            "schema_alias_key": self.SCHEMA_ALIAS_KEY,
            "database_result_key": self.DATABASE_KEY,
            "schema_result_key": self.SCHEMA_KEY,
        }
        return await self.sql_resource.fetch_metadata(args)
