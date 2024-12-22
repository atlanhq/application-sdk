import logging
from typing import Any, Dict, List

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
        FETCH_DATABASES_SQL (str): The SQL query to fetch all databases (for hierarchical mode).
        FETCH_SCHEMAS_SQL (str): The SQL query to fetch schemas for a database (for hierarchical mode).
        USE_HIERARCHICAL_FETCH (bool): Whether to use hierarchical fetching (databases first, then schemas).

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
    USE_HIERARCHICAL_FETCH: bool = False
    FETCH_DATABASES_SQL: str = ""
    FETCH_SCHEMAS_SQL: str = ""

    DATABASE_ALIAS_KEY: str | None = None
    SCHEMA_ALIAS_KEY: str | None = None

    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    sql_resource: SQLResource | None = None

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    @property
    def use_hierarchical_fetch(self) -> bool:
        """
        Determine if hierarchical fetching should be used based on the presence of required SQL queries.
        """
        return bool(self.FETCH_DATABASES_SQL and self.FETCH_SCHEMAS_SQL)

    async def prepare(self, credentials: Dict[str, Any]) -> None:
        self.sql_resource.set_credentials(credentials)
        await self.sql_resource.load()

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")

        if not self.use_hierarchical_fetch or not self.USE_HIERARCHICAL_FETCH:
            # Use the original flat fetching method
            args = {
                "metadata_sql": self.METADATA_SQL,
                "database_alias_key": self.DATABASE_ALIAS_KEY,
                "schema_alias_key": self.SCHEMA_ALIAS_KEY,
                "database_result_key": self.DATABASE_KEY,
                "schema_result_key": self.SCHEMA_KEY,
            }
            return await self.sql_resource.fetch_metadata(args)
        else:
            # Use hierarchical fetching
            return await self.fetch_metadata_hierarchical()

    async def fetch_metadata_hierarchical(self) -> List[Dict[str, str]]:
        """
        Fetch metadata in a hierarchical manner - first databases, then schemas.
        This is useful when the database requires explicit database selection before querying schemas.
        """
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")

        result = []
        try:
            # First fetch all databases
            databases = []
            async for batch in self.sql_resource.run_query(self.FETCH_DATABASES_SQL):
                databases.extend([row[self.DATABASE_KEY] for row in batch])

            # Then for each database, fetch its schemas
            for database in databases:
                # Format the SQL query with the database parameter
                schema_query = self.FETCH_SCHEMAS_SQL.format(database_name=database)
                async for batch in self.sql_resource.run_query(schema_query):
                    for row in batch:
                        result.append({
                            self.DATABASE_KEY: database,
                            self.SCHEMA_KEY: row[self.SCHEMA_KEY]
                        })

        except Exception as e:
            logger.error(f"Failed to fetch metadata hierarchically: {str(e)}")
            raise e

        return result
