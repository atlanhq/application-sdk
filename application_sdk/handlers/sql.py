import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from application_sdk import activity_pd
from application_sdk.app.rest.fastapi.models.workflow import MetadataType
from application_sdk.clients.sql_client import SQLClient
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.handlers import WorkflowHandlerInterface
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class SQLWorkflowHandler(WorkflowHandlerInterface):
    """
    Handler class for SQL workflows
    """

    sql_client: SQLClient | None
    # Variables for testing authentication
    test_authentication_sql: str = "SELECT 1;"
    # Variables for fetching metadata
    metadata_sql: str | None = None
    tables_check_sql: str | None = None
    fetch_databases_sql: str | None = None
    fetch_schemas_sql: str | None = None
    database_alias_key: str = "catalog_name"
    schema_alias_key: str = "schema_name"
    database_result_key: str = "TABLE_CATALOG"
    schema_result_key: str = "TABLE_SCHEMA"

    def __init__(self, sql_client: SQLClient | None = None):
        self.sql_client = sql_client

    async def prepare(self, credentials: Dict[str, Any]) -> None:
        """
        Method to prepare and load the SQL resource
        """
        self.sql_client.set_credentials(credentials)
        await self.sql_client.load()

    @activity_pd(
        batch_input=lambda self, args: self.sql_client.sql_input(
            engine=self.sql_client.engine,
            query=self.metadata_sql,
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
                        self.database_result_key: row[self.database_alias_key],
                        self.schema_result_key: row[self.schema_alias_key],
                    }
                )
        except Exception as exc:
            logger.error(f"Failed to fetch metadata: {str(exc)}")
            raise exc
        return result

    @activity_pd(
        batch_input=lambda self, workflow_args=None: self.sql_client.sql_input(
            self.sql_client.engine, self.test_authentication_sql, chunk_size=None
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

        if not self.sql_client:
            raise ValueError("SQL client is not defined")

        if metadata_type == MetadataType.ALL:
            # Use flat mode for backward compatibility
            args = {}
            result = await self.prepare_metadata(args)
            return result

        else:
            try:
                if metadata_type == MetadataType.DATABASE:
                    return await self.fetch_databases()
                elif metadata_type == MetadataType.SCHEMA:
                    if not database:
                        raise ValueError(
                            "Database must be specified when fetching schemas"
                        )
                    return await self.fetch_schemas(database)
                else:
                    raise ValueError(f"Invalid metadata type: {metadata_type}")
            except Exception as e:
                logger.error(f"Failed to fetch metadata: {str(e)}")
                raise

    async def fetch_databases(self) -> List[Dict[str, str]]:
        """Fetch only database information."""
        if not self.sql_client:
            raise ValueError("SQL Resource not defined")
        databases = []
        async for batch in self.sql_client.run_query(self.fetch_databases_sql):
            for row in batch:
                databases.append(
                    {self.database_result_key: row[self.database_result_key]}
                )
        return databases

    async def fetch_schemas(self, database: str) -> List[Dict[str, str]]:
        """Fetch schemas for a specific database."""
        if not self.sql_client:
            raise ValueError("SQL Resource not defined")
        schemas = []
        schema_query = self.fetch_schemas_sql.format(database_name=database)
        async for batch in self.sql_client.run_query(schema_query):
            for row in batch:
                schemas.append(
                    {
                        self.database_result_key: database,
                        self.schema_result_key: row[self.schema_result_key],
                    }
                )
        return schemas

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
            schemas_results: List[Dict[str, str]] = await self.prepare_metadata({})

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
            allowed_databases.add(schema[self.database_result_key])
            allowed_schemas.add(
                f"{schema[self.database_result_key]}.{schema[self.schema_result_key]}"
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
        batch_input=lambda self, workflow_args: self.sql_client.sql_input(
            engine=self.sql_client.engine,
            query=SQLWorkflow.prepare_query(
                query=self.tables_check_sql, workflow_args=workflow_args
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
