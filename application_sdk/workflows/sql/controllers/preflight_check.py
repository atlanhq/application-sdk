import asyncio
import contextvars
import json
import logging
import os
from typing import Any, Dict, List, Set, Tuple

import pandas as pd

from application_sdk import activity_pd
from application_sdk.workflows.controllers import (
    WorkflowPreflightCheckControllerInterface,
)
from application_sdk.workflows.sql.resources.sql_resource import SQLResource
from application_sdk.workflows.sql.workflows.workflow import SQLWorkflow

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

            if (
                not results["databaseSchemaCheck"]["success"]
                or not results["tablesCheck"]["success"]
            ):
                raise ValueError(
                    f"Preflight check failed, databaseSchemaCheck: {results['databaseSchemaCheck']}, tablesCheck: {results['tablesCheck']}"
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
        batch_input=lambda self, workflow_args: self.sql_resource.sql_input(
            self.sql_resource.engine,
            SQLWorkflow.prepare_query(
                query=self.TABLES_CHECK_SQL, workflow_args=workflow_args
            ),
            chunk_size=None,
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


class SQLDatabaseWorkflowPreflightCheckController(SQLWorkflowPreflightCheckController):
    """
    SQL Database Workflow Preflight Check Interface

    This interface is used to perform preflight checks on the SQL Database workflow.

    Attributes:
        METADATA_SQL (str): The SQL query to fetch the metadata.
        TABLES_CHECK_SQL (str): The SQL query to fetch the tables.
        DATABASE_KEY (str): The key to fetch the database name.
        SCHEMA_KEY (str): The key to fetch the schema name.
    """

    semaphore_concurrency: int = int(os.getenv("SEMAPHORE_CONCURRENCY", 5))

    # Create a context variable to hold the semaphore
    semaphore_context = contextvars.ContextVar("semaphore")

    async def get_full_databases(self):
        get_databases_query = """
            SHOW DATABASES;
        """
        get_databases_input = self.sql_resource.sql_input(
            engine=self.sql_resource.engine,
            query=get_databases_query,
        )

        get_databases_result = await get_databases_input.get_batched_dataframe()

        full_database_list = []

        for batch in get_databases_result:
            if isinstance(batch, pd.DataFrame):
                full_database_list.append(batch)
            else:
                for df in batch:
                    full_database_list.append(df)

        return pd.concat(full_database_list, ignore_index=True)

    async def filter_databases_list(
        self,
        db_name: str,
        payload: Dict[str, Any],
        query: str,
        semaphore: asyncio.Semaphore,
    ):
        """
        Fetch data (schema, table, or column) for a single database and return the results as a DataFrame.
        Generalized function for schema, tables, and columns.
        """
        async with semaphore:
            # Update the workflow_args with the current database name for fetching
            payload["database_name"] = db_name

            prepared_query = SQLWorkflow.prepare_query(
                query=query, workflow_args=payload
            )

            filter_databases_input = self.sql_resource.sql_input(
                engine=self.sql_resource.engine,
                query=prepared_query,
            )

            return await filter_databases_input.get_dataframe()

    async def fetch_all_filtered_databases(self, payload: Dict[str, Any]) -> List[str]:
        databases = await self.get_full_databases()
        full_database_list = databases["name"].tolist()
        database_filter_query = """
            SELECT D.CATALOG_NAME AS DATABASE_NAME
            FROM "{database_name}"."INFORMATION_SCHEMA"."SCHEMATA" AS D
            WHERE
                concat(D.CATALOG_NAME, concat('.', D.SCHEMA_NAME)) NOT REGEXP '{normalized_exclude_regex}'
                AND concat(D.CATALOG_NAME, concat('.', D.SCHEMA_NAME)) REGEXP '{normalized_include_regex}'
            GROUP BY D.CATALOG_NAME;
        """
        # Create a semaphore if not present in the context
        semaphore = self.semaphore_context.get(
            asyncio.Semaphore(self.semaphore_concurrency)
        )

        # Create a list of tasks to fetch schemas concurrently
        execute_queries = [
            self.filter_databases_list(
                db_name, payload, database_filter_query, semaphore
            )
            for db_name in full_database_list
        ]

        # Use asyncio.gather to execute all database fetches concurrently
        results = await asyncio.gather(*execute_queries)

        filter_database_list: List[pd.DataFrame] = []

        for result in results:
            if isinstance(result, pd.DataFrame):
                filter_database_list.append(result)

        return pd.concat(filter_database_list, ignore_index=True)

    async def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Starting SQL Database workflow preflight check")
        results: Dict[str, Any] = {}
        try:
            # Extract the list of databases from the payload
            database_list = payload.get("database_list", [])

            if not database_list:
                # Fallback: Retrieve all available databases if none provided
                logger.info(
                    "No database list provided. Fetching all databases and filtering."
                )
                filtered_databases = await self.fetch_all_filtered_databases(payload)
                database_list = filtered_databases["database_name"].tolist()

            # Create a semaphore if not present in the context
            semaphore = self.semaphore_context.get(
                asyncio.Semaphore(self.semaphore_concurrency)
            )

            # Run schemas and databases checks for each database concurrently
            preflight_schemas_and_databases = [
                self.db_check_schemas_and_databases(
                    payload,
                    db_name,
                    semaphore,
                )
                for db_name in database_list
            ]
            preflight_schemas_and_databases_results = await asyncio.gather(
                *preflight_schemas_and_databases, return_exceptions=True
            )
            results["databaseSchemaCheck"] = preflight_schemas_and_databases_results

            # Run tables check for each database concurrently
            preflight_tables_check = [
                self.db_tables_check(
                    payload,
                    db_name,
                    semaphore,
                )
                for db_name in database_list
            ]
            preflight_tables_check_results = await asyncio.gather(
                *preflight_tables_check, return_exceptions=True
            )
            results["tablesCheck"] = preflight_tables_check_results

            # Check if any of the results were unsuccessful
            database_schema_check_success = all(
                check["success"] for check in preflight_schemas_and_databases_results
            )
            tables_check_success = all(
                check["success"] for check in preflight_tables_check_results
            )

            if not database_schema_check_success or not tables_check_success:
                raise ValueError(
                    f"Preflight check failed, databaseSchemaCheck: {preflight_schemas_and_databases_results}, tablesCheck: {preflight_tables_check_results}"
                )

            logger.info("Preflight check completed successfully")
        except Exception as e:
            logger.error("Error during preflight check", exc_info=True)
            results["error"] = f"Preflight check failed: {str(e)}"
        return results

    @staticmethod
    def validate_filters(
        include_filter: Dict[str, List[str]],
        allowed_databases: Set[str],
        allowed_schemas: Set[str],
    ) -> Tuple[bool, str]:
        """
        Validate the include_filter for a specific database and schema.
        This ensures that the filter is applied only for the current database.
        """
        # Iterate through include_filter items (database and their associated schemas)
        for filtered_db, filtered_schemas in include_filter.items():
            database_name = filtered_db.strip("^$")
            # Check if the database exists in the allowed databases
            if database_name in allowed_databases:
                # Loop through each schema in the filtered list
                for schema in filtered_schemas:
                    schema = schema.strip("^$")
                    # Check if the database.schema combination exists in allowed_schemas
                    if f"{database_name}.{schema}" not in allowed_schemas:
                        return False, f"{database_name}.{schema} schema"
                # If all schemas for this database are valid
                return True, ""

        return False, f"{database_name} database"

    async def db_fetch_metadata(self, database_name: str) -> List[Dict[str, str]]:
        query = self.METADATA_SQL.format(database_name=database_name)
        if not self.sql_resource:
            raise ValueError("SQL Resource not defined")
        args = {
            "metadata_sql": query,
            "database_alias_key": self.DATABASE_ALIAS_KEY,
            "schema_alias_key": self.SCHEMA_ALIAS_KEY,
            "database_result_key": self.DATABASE_KEY,
            "schema_result_key": self.SCHEMA_KEY,
        }
        return await self.sql_resource.fetch_metadata(args)

    async def db_check_schemas_and_databases(
        self, payload: Dict[str, Any], database_name: str, semaphor: asyncio.Semaphore
    ) -> Dict[str, Any]:
        async with semaphor:
            logger.info(f"Starting schema and database check for {database_name}")
            try:
                schemas_results: List[Dict[str, str]] = await self.db_fetch_metadata(
                    database_name
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
                    "database_name": database_name,
                    "success": check_success,
                    "successMessage": f"Schemas and Databases check successful for {database_name}"
                    if check_success
                    else "",
                    "failureMessage": f"Schemas and Databases check failed for {missing_object_name}"
                    if not check_success
                    else "",
                }
            except Exception as e:
                logger.error(
                    f"Error during schema and database check for {database_name}",
                    exc_info=True,
                )
                return {
                    "database_name": database_name,
                    "success": False,
                    "successMessage": "",
                    "failureMessage": f"Schemas and Databases check failed for {database_name}",
                    "error": str(e),
                }

    async def db_tables_check(
        self, payload: Dict[str, Any], database_name: str, semaphor: asyncio.Semaphore
    ) -> Dict[str, Any]:
        async with semaphor:
            logger.info(f"Starting tables check for {database_name}")
            try:
                payload["database_name"] = database_name
                tables_check_result = await self.sql_resource.sql_input(
                    engine=self.sql_resource.engine,
                    query=SQLWorkflow.prepare_query(
                        query=self.TABLES_CHECK_SQL, workflow_args=payload
                    ),
                    chunk_size=None,
                ).get_dataframe()

                result = 0
                for row in tables_check_result.to_dict(orient="records"):
                    result += row["count"]

                return {
                    "database_name": database_name,
                    "success": True,
                    "successMessage": f"Tables check successful for {database_name}. Table count: {result}",
                    "failureMessage": "",
                }
            except Exception as e:
                logger.error("Error during tables check", exc_info=True)
                return {
                    "database_name": database_name,
                    "success": False,
                    "successMessage": "",
                    "failureMessage": f"Tables check failed for {database_name}",
                    "error": str(e),
                }
