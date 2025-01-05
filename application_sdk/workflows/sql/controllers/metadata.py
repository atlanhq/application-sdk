import logging
from typing import Any, Dict, List, Optional

from application_sdk.app.rest.fastapi.models.workflow import MetadataType
from application_sdk.workflows.controllers import WorkflowMetadataControllerInterface
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowMetadataController(WorkflowMetadataControllerInterface):
    """
    SQL Workflow Metadata Interface

    This interface is used to fetch metadata from the database.

    Attributes:
        METADATA_SQL (str): The SQL query to fetch both the database and the schemas.
        FETCH_DATABASES_SQL (str): The SQL query to fetch all databases.
        FETCH_SCHEMAS_SQL (str): The SQL query to fetch schemas for a given database.
        DATABASE_KEY (str): The key to fetch the database name.
        SCHEMA_KEY (str): The key to fetch the schema name.

    Usage:
        Subclass this interface and implement the required attributes and any methods
        that need custom behavior (ex. fetch_metadata).

        >>> class MySQLWorkflowMetadataInterface(SQLWorkflowMetadataInterface):
        >>>     METADATA_SQL = "SELECT * FROM information_schema.schemata"
        >>>     FETCH_DATABASES_SQL = "SELECT * FROM information_schema.schemata"
        >>>     FETCH_SCHEMAS_SQL = "SELECT * FROM information_schema.schemata where TABLE_CATALOG = {database_name}"
        >>>     DATABASE_KEY = "TABLE_CATALOG"
        >>>     SCHEMA_KEY = "TABLE_SCHEMA"
        >>>     def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        >>>         super().__init__(create_engine_fn)
    """

    METADATA_SQL: str = ""
    FETCH_DATABASES_SQL: str = ""
    FETCH_SCHEMAS_SQL: str = ""

    DATABASE_ALIAS_KEY: str | None = None
    SCHEMA_ALIAS_KEY: str | None = None

    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    sql_resource: SQLResource | None = None

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    async def prepare(self, credentials: Dict[str, Any]) -> None:
        """Prepare the SQL resource with credentials from the request."""
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")
        self.sql_resource.set_credentials(credentials)
        await self.sql_resource.load()

    async def fetch_metadata(
        self,
        metadata_type: Optional[MetadataType] = None,
        database: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Fetch metadata based on the requested type.
        Args:
            metadata_type: Optional type of metadata to fetch (database or schema)
            database: Optional database name when fetching schemas
        Returns:
            List of metadata dictionaries
        Raises:
            ValueError: If metadata_type is invalid or if database is required but not provided
        """

        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")

        if metadata_type == MetadataType.ALL:
            # Use flat mode for backward compatibility
            args = {
                "metadata_sql": self.METADATA_SQL,
                "database_alias_key": self.DATABASE_ALIAS_KEY,
                "schema_alias_key": self.SCHEMA_ALIAS_KEY,
                "database_result_key": self.DATABASE_KEY,
                "schema_result_key": self.SCHEMA_KEY,
            }
            result =  await self.sql_resource.fetch_metadata(args)
            return result

        else:
            try:
                if metadata_type == MetadataType.DATABASE:
                    return await self.fetch_databases()
                elif metadata_type == MetadataType.SCHEMA:
                    if not database:
                        raise ValueError("Database must be specified when fetching schemas")
                    return await self.fetch_schemas(database)
                else:
                    raise ValueError(f"Invalid metadata type: {metadata_type}")
            except Exception as e:
                logger.error(f"Failed to fetch metadata: {str(e)}")
                raise

    async def fetch_databases(self) -> List[Dict[str, str]]:
        """Fetch only database information."""
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")
        databases = []
        async for batch in self.sql_resource.run_query(self.FETCH_DATABASES_SQL):
            for row in batch:
                databases.append({self.DATABASE_KEY: row[self.DATABASE_KEY]})
        return databases

    async def fetch_schemas(self, database: str) -> List[Dict[str, str]]:
        """Fetch schemas for a specific database."""
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")
        schemas = []
        schema_query = self.FETCH_SCHEMAS_SQL.format(database_name=database)
        async for batch in self.sql_resource.run_query(schema_query):
            for row in batch:
                schemas.append(
                    {self.DATABASE_KEY: database, self.SCHEMA_KEY: row[self.SCHEMA_KEY]}
                )
        return schemas
