import asyncio
import json
import logging
from typing import Any, Dict, List, Set, Tuple

import pandas as pd

from application_sdk import activity_pd
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers import WorkflowHandlerInterface
from application_sdk.workflows.sql.resources.sql_resource import SQLResource
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLWorkflowHandler(WorkflowHandlerInterface):
    """
    Handler class for SQL workflows
    """

    sql_resource: SQLResource | None
    # Variables for testing authentication
    TEST_AUTHENTICATION_SQL: str = "SELECT 1;"
    # Variables for fetching metadata
    METADATA_SQL: str | None = None
    TABLES_CHECK_SQL: str | None = None
    DATABASE_ALIAS_KEY: str = "catalog_name"
    SCHEMA_ALIAS_KEY: str = "schema_name"
    DATABASE_RESULT_KEY: str = "TABLE_CATALOG"
    SCHEMA_RESULT_KEY: str = "TABLE_SCHEMA"

    def __init__(self, sql_resource: SQLResource | None = None):
        self.sql_resource = sql_resource

    async def prepare(self, credentials: Dict[str, Any]) -> None:
        """
        Method to prepare and load the SQL resource
        """
        self.sql_resource.set_credentials(credentials)
        await self.sql_resource.load()

    @activity_pd(
        batch_input=lambda self, args: self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=self.METADATA_SQL,
            chunk_size=None,
        )
    )
    async def prepare_metadata(
        self,
        batch_input: pd.DataFrame,
        **kwargs,
    ) -> List[Dict[Any, Any]]:
        """
        Method to fetch and prepare the databases and schemas metadata
        """
        result: List[Dict[Any, Any]] = []
        try:
            for row in batch_input.to_dict(orient="records"):
                result.append(
                    {
                        self.DATABASE_RESULT_KEY: row[self.DATABASE_ALIAS_KEY],
                        self.SCHEMA_RESULT_KEY: row[self.SCHEMA_ALIAS_KEY],
                    }
                )
        except Exception as exc:
            logger.error(f"Failed to fetch metadata: {str(exc)}")
            raise exc
        return result

    @activity_pd(
        batch_input=lambda self, workflow_args=None: self.sql_resource.sql_input(
            self.sql_resource.engine, self.TEST_AUTHENTICATION_SQL, chunk_size=None
        )
    )
    async def test_auth(self, batch_input: pd.DataFrame, **kwargs) -> bool:
        """
        Test the authentication credentials.

        :return: True if the credentials are valid, False otherwise.
        :raises Exception: If the credentials are invalid.
        """
        try:
            batch_input.to_dict(orient="records")
            return True
        except Exception as exc:
            logger.error(
                f"Failed to authenticate with the given credentials: {str(exc)}"
            )
            raise exc

    async def fetch_metadata(self) -> List[Dict[str, str]]:
        """
        Method to fetch metadata
        """
        if not self.sql_resource:
            raise ValueError("SQL client is not defined")
        args = {}
        return await self.prepare_metadata(args)

    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Method to perform preflight checks
        """
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

            if (
                not results["databaseSchemaCheck"]["success"]
                or not results["tablesCheck"]["success"]
            ):
                raise ValueError(
                    f"Preflight check failed, databaseSchemaCheck: {results['databaseSchemaCheck']}, tablesCheck: {results['tablesCheck']}"
                )

            logger.info("Preflight check completed successfully")
        except Exception as exc:
            logger.error("Error during preflight check", exc_info=True)
            results["error"] = f"Preflight check failed: {str(exc)}"
        return results

    async def check_schemas_and_databases(
        self, payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        logger.info("Starting schema and database check")
        """
        Method to check the schemas and databases
        """
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
        except Exception as exc:
            logger.error("Error during schema and database check", exc_info=True)
            return {
                "success": False,
                "successMessage": "",
                "failureMessage": "Schemas and Databases check failed",
                "error": str(exc),
            }

    def extract_allowed_schemas(
        self,
        schemas_results: List[Dict[str, str]],
    ) -> Tuple[Set[str], Set[str]]:
        """
        Method to extract the allowed databases and schemas
        """
        allowed_databases: Set[str] = set()
        allowed_schemas: Set[str] = set()
        for schema in schemas_results:
            allowed_databases.add(schema[self.DATABASE_RESULT_KEY])
            allowed_schemas.add(
                f"{schema[self.DATABASE_RESULT_KEY]}.{schema[self.SCHEMA_RESULT_KEY]}"
            )
        return allowed_databases, allowed_schemas

    @staticmethod
    def validate_filters(
        include_filter: Dict[str, List[str] | str],
        allowed_databases: Set[str],
        allowed_schemas: Set[str],
    ) -> Tuple[bool, str]:
        """
        Method to valudate the filters
        """
        for filtered_db, filtered_schemas in include_filter.items():
            db = filtered_db.strip("^$")
            if db not in allowed_databases:
                return False, f"{db} database"

            # Handle wildcard case
            if filtered_schemas == "*":
                continue

            # Handle list case
            if isinstance(filtered_schemas, list):
                for schema in filtered_schemas:
                    sch = schema.strip("^$")
                    if f"{db}.{sch}" not in allowed_schemas:
                        return False, f"{db}.{sch} schema"
        return True, ""

    @activity_pd(
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=SQLWorkflow.prepare_query(
                query=self.TABLES_CHECK_SQL, workflow_args=workflow_args
            ),
            chunk_size=None,
        )
    )
    async def tables_check(self, batch_input, **kwargs) -> Dict[str, Any]:
        """
        Method to check the count of tables
        """
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
        except Exception as exc:
            logger.error("Error during tables check", exc_info=True)
            return {
                "success": False,
                "successMessage": "",
                "failureMessage": "Tables check failed",
                "error": str(exc),
            }
