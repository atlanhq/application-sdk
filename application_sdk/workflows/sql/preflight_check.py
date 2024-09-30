import json
import logging
from typing import Any, Dict, List, Set, Tuple, Callable

from sqlalchemy import Engine, text

from application_sdk.workflows.models.preflight import PreflightPayload
from application_sdk.workflows import WorkflowPreflightCheckInterface
from application_sdk.workflows.sql.utils import prepare_filters

logger = logging.getLogger(__name__)


class SQLWorkflowPreflightCheckInterface(WorkflowPreflightCheckInterface):
    METADATA_SQL = ""
    TABLES_CHECK_SQL = ""
    DATABASE_KEY = "TABLE_CATALOG"
    SCHEMA_KEY = "TABLE_SCHEMA"

    def __init__(self, create_engine_fn: Callable[[Dict[str, Any]], Engine]):
        self.create_engine_fn = create_engine_fn


    def preflight_check(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        preflight_payload = PreflightPayload(**payload)
        logger.info("Starting preflight check")
        results: Dict[str, Any] = {}
        try:
            results["databaseSchemaCheck"] = self.check_schemas_and_databases(
                preflight_payload
            )
            results["tablesCheck"] = self.tables_check(preflight_payload)
            logger.info("Preflight check completed successfully")
        except Exception as e:
            logger.error("Error during preflight check", exc_info=True)
            results["error"] = f"Preflight check failed: {str(e)}"
        return results

    # FIXME: duplicate with SQLWorkflowMetadataInterface
    def fetch_metadata(self, credential: Dict) -> List[Dict[str, str]]:
        connection = None
        cursor = None
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
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

        return result

    def check_schemas_and_databases(self, payload: PreflightPayload) -> Dict[str, Any]:
        logger.info("Starting schema and database check")
        connection = None
        try:
            schemas_results: List[Dict[str, str]] = self.fetch_metadata(
                payload.credentials
            )

            include_filter = json.loads(payload.form_data.include_filter)
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
        finally:
            if connection:
                connection.close()

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

    def tables_check(self, payload: PreflightPayload) -> Dict[str, Any]:
        logger.info("Starting tables check")
        connection = None
        try:
            normalized_include_regex, normalized_exclude_regex, exclude_table = (
                prepare_filters(
                    payload.form_data.include_filter,
                    payload.form_data.exclude_filter,
                    payload.form_data.temp_table_regex,
                )
            )
            query = self.TABLES_CHECK_SQL.format(
                exclude_table=exclude_table,
                normalized_exclude_regex=normalized_exclude_regex,
                normalized_include_regex=normalized_include_regex,
            )

            credentials = payload.credentials
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
        finally:
            if connection:
                connection.close()
