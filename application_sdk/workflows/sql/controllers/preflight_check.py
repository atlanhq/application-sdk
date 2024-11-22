import asyncio
import json
import logging
from typing import Any, Dict, List, Set, Tuple

from application_sdk import activity_pd
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.sql.resources.sql_resource import SQLResource

logger = logging.getLogger(__name__)


class SQLWorkflowPreflightCheckController(WorkflowPreflightCheckControllerInterface):
    """
    SQL Workflow Preflight Check Interface

    This interface is used to perform preflight checks on the SQL workflow.

    Attributes:
        METADATA_SQL (str): The SQL query to fetch the metadata.
        TABLES_CHECK_SQL (str): The SQL query to fetch the tables.
        DATABASE_KEY (str): The key to fetch the database name.
        SCHEMA_KEY (str): The key to fetch the schema name.

    Usage:
        Subclass this interface and implement the required attributes and any methods
        that need custom behavior.

        >>> class MySQLWorkflowPreflightCheckInterface(SQLWorkflowPreflightCheckInterface):
        >>>     METADATA_SQL = "SELECT * FROM information_schema.tables"
        >>>     TABLES_CHECK_SQL = "SELECT COUNT(*) FROM information_schema.tables"
        >>>     DATABASE_KEY = "TABLE_CATALOG"
        >>>     SCHEMA_KEY = "TABLE_SCHEMA"
        >>>     def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        >>>         super().__init__(create_engine_fn)
    """

    METADATA_SQL: str = ""
    TABLES_CHECK_SQL: str = ""

    DATABASE_ALIAS_KEY: str | None = None
    SCHEMA_ALIAS_KEY: str | None = None

    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting preflight check")
        results: Dict[str, Any] = {}
        try:
            (
                results["databaseSchemaCheck"],
                results["tablesCheck"],
            ) = await asyncio.gather(
                self.check_schemas_and_databases(payload),
                self.tables_check(payload),
            )
            logger.info("Preflight check completed successfully")
        except Exception as e:
            logger.error("Error during preflight check", exc_info=True)
            results["error"] = f"Preflight check failed: {str(e)}"
        return results

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")
        args = {
            "metadata_sql": self.METADATA_SQL,
            "database_alias_key": self.DATABASE_ALIAS_KEY,
            "schema_alias_key": self.SCHEMA_ALIAS_KEY,
            "database_result_key": self.DATABASE_KEY,
            "schema_result_key": self.SCHEMA_KEY,
        }
        return await self.sql_resource.fetch_metadata(args)

    async def check_schemas_and_databases(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        logger.info("Starting schema and database check")
        try:
            schemas_results: List[Dict[str, str]] = await self.fetch_metadata()

            include_filter = json.loads(
                payload.get("form_data", {}).get("include_filter", "{}")
            )
            allowed_databases, allowed_schemas = self.extract_allowed_schemas(
                schemas_results
            )
            check_success, missing_object_name = self.validate_filters(
                include_filter, allowed_databases, allowed_schemas
            )

            return {
                "success": check_success,
                "successMessage": "Schemas and Databases check successful"
                if check_success
                else "",
                "failureMessage": f"Schemas and Databases check failed for {missing_object_name}"
                if not check_success
                else "",
            }
        except Exception as e:
            logger.error("Error during schema and database check", exc_info=True)
            return {
                "success": False,
                "successMessage": "",
                "failureMessage": "Schemas and Databases check failed",
                "error": str(e),
            }

    def extract_allowed_schemas(
        self,
        schemas_results: List[Dict[str, str]],
    ) -> Tuple[Set[str], Set[str]]:
        allowed_databases: Set[str] = set()
        allowed_schemas: Set[str] = set()
        for schema in schemas_results:
            allowed_databases.add(schema[self.DATABASE_KEY])
            allowed_schemas.add(
                f"{schema[self.DATABASE_KEY]}.{schema[self.SCHEMA_KEY]}"
            )
        return allowed_databases, allowed_schemas

    @staticmethod
    def validate_filters(
        include_filter: Dict[str, List[str]],
        allowed_databases: Set[str],
        allowed_schemas: Set[str],
    ) -> Tuple[bool, str]:
        for filtered_db, filtered_schemas in include_filter.items():
            db = filtered_db.strip("^$")
            if db not in allowed_databases:
                return False, f"{db} database"
            for schema in filtered_schemas:
                sch = schema.strip("^$")
                if f"{db}.{sch}" not in allowed_schemas:
                    return False, f"{db}.{sch} schema"
        return True, ""

    @activity_pd(
        batch_input=lambda self, args: self.sql_resource.sql_input(
            self.sql_resource.engine, self.TABLES_CHECK_SQL
        )
    )
    async def tables_check(self, batch_input, **kwargs) -> Dict[str, Any]:
        logger.info("Starting tables check")
        try:
            result = 0
            for row in batch_input.to_dict(orient="records"):
                result += row["count"]

            return {
                "success": True,
                "successMessage": f"Tables check successful. Table count: {result}",
                "failureMessage": "",
            }
        except Exception as e:
            logger.error("Error during tables check", exc_info=True)
            return {
                "success": False,
                "successMessage": "",
                "failureMessage": "Tables check failed",
                "error": str(e),
            }
