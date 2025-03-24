import asyncio
import json
import os
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from packaging import version

from application_sdk.application.fastapi.models import MetadataType
from application_sdk.clients.sql import SQLClient
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.decorators import transform
from application_sdk.handlers import HandlerInterface
from application_sdk.inputs.sql_query import SQLQueryInput

logger = get_logger(__name__)


class SQLConstants(Enum):
    """
    Constants for SQL handler
    """

    DATABASE_ALIAS_KEY = "catalog_name"
    SCHEMA_ALIAS_KEY = "schema_name"
    DATABASE_RESULT_KEY = "TABLE_CATALOG"
    SCHEMA_RESULT_KEY = "TABLE_SCHEMA"


class SQLHandler(HandlerInterface):
    """
    Handler class for SQL workflows
    """

    sql_client: SQLClient
    # Variables for testing authentication
    test_authentication_sql: str = "SELECT 1;"
    get_client_version_sql: str | None = None
    # Variables for fetching metadata
    metadata_sql: str | None = None
    tables_check_sql: str | None = None
    fetch_databases_sql: str | None = None
    fetch_schemas_sql: str | None = None
    database_alias_key: str = SQLConstants.DATABASE_ALIAS_KEY.value
    schema_alias_key: str = SQLConstants.SCHEMA_ALIAS_KEY.value
    database_result_key: str = SQLConstants.DATABASE_RESULT_KEY.value
    schema_result_key: str = SQLConstants.SCHEMA_RESULT_KEY.value

    temp_table_regex_sql: str = ""

    def __init__(self, sql_client: SQLClient | None = None):
        self.sql_client = sql_client

    async def load(self, credentials: Dict[str, Any]) -> None:
        """
        Method to load and load the SQL client
        """
        await self.sql_client.load(credentials)

    @transform(sql_input=SQLQueryInput(query="metadata_sql", chunk_size=None))
    async def prepare_metadata(
        self,
        sql_input: pd.DataFrame,
        **kwargs: Dict[str, Any],
    ) -> List[Dict[Any, Any]]:
        """
        Method to fetch and prepare the databases and schemas metadata
        """
        result: List[Dict[Any, Any]] = []
        try:
            for row in sql_input.to_dict(orient="records"):
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

    @transform(
        sql_input=SQLQueryInput(query="test_authentication_sql", chunk_size=None)
    )
    async def test_auth(
        self,
        sql_input: pd.DataFrame,
        **kwargs: Dict[str, Any],
    ) -> bool:
        """
        Test the authentication credentials.

        :return: True if the credentials are valid, False otherwise.
        :raises Exception: If the credentials are invalid.
        """
        try:
            sql_input.to_dict(orient="records")
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
            result = await self.prepare_metadata()
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
            raise ValueError("SQL Client not defined")
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
            raise ValueError("SQL Client not defined")
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
                results["versionCheck"],
            ) = await asyncio.gather(
                self.check_schemas_and_databases(payload),
                self.tables_check(payload),
                self.check_client_version(),
            )

            if (
                not results["databaseSchemaCheck"]["success"]
                or not results["tablesCheck"]["success"]
                or not results["versionCheck"]["success"]
            ):
                raise ValueError(
                    f"Preflight check failed, databaseSchemaCheck: {results['databaseSchemaCheck']}, "
                    f"tablesCheck: {results['tablesCheck']}, "
                    f"versionCheck: {results['versionCheck']}"
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
            schemas_results: List[Dict[str, str]] = await self.prepare_metadata()

            include_filter = json.loads(
                payload.get("metadata", {}).get("include-filter", "{}")
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

    @transform(
        sql_input=SQLQueryInput(
            query="tables_check_sql",
            chunk_size=None,
            temp_table_sql_query="temp_table_regex_sql",
        )
    )
    async def tables_check(
        self,
        sql_input: pd.DataFrame,
        **kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Method to check the count of tables
        """
        logger.info("Starting tables check")
        try:
            result = 0
            for row in sql_input.to_dict(orient="records"):
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

    async def check_client_version(self) -> Dict[str, Any]:
        """
        Check if the client version meets the minimum required version.

        If get_client_version_sql is not defined by the implementing app,
        this check will be skipped and return success.

        Returns:
            Dict[str, Any]: Result of the version check with success status and messages
        """

        logger.info("Checking client version")
        try:
            # If server_version_info is None, try using get_client_version_sql
            if not self.get_client_version_sql:
                logger.info("Client version check skipped - no version query defined")
                return {
                    "success": True,
                    "successMessage": "Client version check skipped - no version query defined",
                    "failureMessage": "",
                }

            # Try to get the version from the sql_client
            version_info = self.sql_client.engine.dialect.server_version_info
            min_version = os.getenv("ATLAN_SQL_CLIENT_MIN_VERSION", "0.0.0")

            if version_info:
                # Handle tuple version info (like (15, 4))
                client_version = ".".join(str(x) for x in version_info)
            else:
                # Only execute the query if get_client_version_sql is defined
                sql_input = await SQLQueryInput(
                    query=self.get_client_version_sql,
                    engine=self.sql_client.engine,
                    chunk_size=None,
                ).get_dataframe()

                version_string = next(
                    iter(sql_input.to_dict(orient="records")[0].values())
                )
                version_match = re.search(r"(\d+\.\d+(?:\.\d+)?)", version_string)
                if version_match:
                    client_version = version_match.group(1)
                else:
                    client_version = "0.0.0"
                    logger.warning(
                        f"Could not extract version number from: {version_string}"
                    )

            is_valid = version.parse(client_version) >= version.parse(min_version)

            return {
                "success": is_valid,
                "successMessage": f"Client version {client_version} meets minimum required version {min_version}"
                if is_valid
                else "",
                "failureMessage": f"Client version {client_version} does not meet minimum required version {min_version}"
                if not is_valid
                else "",
            }
        except Exception as exc:
            logger.error(f"Error during client version check: {exc}", exc_info=True)
            return {
                "success": False,
                "successMessage": "",
                "failureMessage": "Client version check failed",
                "error": str(exc),
            }
