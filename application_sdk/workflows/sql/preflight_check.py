import json
import logging
from typing import Any, Callable, Dict, List, Set, Tuple

from sqlalchemy import Engine, text

from application_sdk.workflows import WorkflowPreflightCheckInterface
from application_sdk.workflows.sql.utils import prepare_filters

logger = logging.getLogger(__name__)


class SQLWorkflowPreflightCheckInterface(WorkflowPreflightCheckInterface):
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
    DATABASE_KEY: str = "TABLE_CATALOG"
    SCHEMA_KEY: str = "TABLE_SCHEMA"

    def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        self.create_engine_fn = create_engine_fn

    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting preflight check")
        results: Dict[str, Any] = {}
        try:
            results["databaseSchemaCheck"] = self.check_schemas_and_databases(payload)
            results["tablesCheck"] = self.tables_check(payload)
            logger.info("Preflight check completed successfully")
        except Exception as e:
            logger.error("Error during preflight check", exc_info=True)
            results["error"] = f"Preflight check failed: {str(e)}"
        return results

    # FIXME: duplicate with SQLWorkflowMetadataInterface
    def fetch_metadata(self, credential: Dict) -> List[Dict[str, str]]:
        try:
            engine = self.create_engine_fn(credential)
            with engine.connect() as connection:
                cursor = connection.execute(text(self.METADATA_SQL))
                result: List[Dict[str, str]] = []
                while True:
                    rows = cursor.fetchmany(1000)  # Fetch 1000 rows at a time
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

    def check_schemas_and_databases(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting schema and database check")
        try:
            schemas_results: List[Dict[str, str]] = self.fetch_metadata(
                payload.get("credentials", {})
            )

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

    def tables_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting tables check")
        try:
            normalized_include_regex, normalized_exclude_regex, exclude_table = (
                prepare_filters(
                    payload.get("form_data", {}).get("include_filter", ""),
                    payload.get("form_data", {}).get("exclude_filter", ""),
                    payload.get("form_data", {}).get("temp_table_regex", ""),
                )
            )
            query = self.TABLES_CHECK_SQL.format(
                exclude_table=exclude_table,
                normalized_exclude_regex=normalized_exclude_regex,
                normalized_include_regex=normalized_include_regex,
            )

            credentials = payload.get("credentials", {})
            engine = self.create_engine_fn(credentials)
            with engine.connect() as connection:
                cursor = connection.execute(text(query))
                result = cursor.fetchone()[0]

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
