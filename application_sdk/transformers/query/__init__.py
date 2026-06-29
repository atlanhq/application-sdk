from __future__ import annotations

import textwrap
import warnings
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import yaml
from pyatlan.model.enums import AtlanConnectorType

if TYPE_CHECKING:
    import pyarrow as pa

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.common.utils import (
    flatten_yaml_columns,
    get_yaml_query_template_path_mappings,
)
from application_sdk.transformers.query.errors import (
    BuildStructLevelRequiredError as BuildStructLevelRequiredError,
)
from application_sdk.transformers.query.errors import (
    BuildStructPrefixRequiredError as BuildStructPrefixRequiredError,
)
from application_sdk.transformers.query.errors import (
    SqlTransformNotRegisteredError as SqlTransformNotRegisteredError,
)

warnings.warn(
    "application_sdk.transformers.query is deprecated; use the connector-side "
    "asset-mapper pattern (typed records → map_<entity>() → pyatlan_v9 Asset) instead "
    "— will be removed in v4.0. See docs/upgrade-guide-v3.md.",
    DeprecationWarning,
    stacklevel=2,
)

logger = get_logger(__name__)


class QueryBasedTransformer(TransformerInterface):
    """Query based transformer that uses YAML files for SQL queries and DuckDB for execution.

    Uses a YAML file to define SQL queries for each asset type and executes them on raw dataframes
    using DuckDB to get transformed data.

    The execution flow is:
        1. Initialize transformer with connector name and tenant ID
        2. Map asset types (DATABASE, SCHEMA, TABLE, COLUMN etc) to YAML template paths
           from default or custom template directories
        3. Transform metadata by:
           - Loading YAML template for the typename
           - Preparing default attributes and SQL template
           - Generating SQL query from template
           - Executing query on raw pyarrow Table via DuckDB
           - Converting flat table with dot notation to nested structure
           - Returning transformed list of dicts

    Args:
        connector_name: Name of the connector
        tenant_id: ID of the tenant
        **kwargs: Additional keyword arguments

    .. deprecated:: 3.20.0
        Use the connector-side asset-mapper pattern (typed records →
        ``map_<entity>()`` → ``pyatlan_v9`` Asset) instead — will be removed in
        v4.0. See ``docs/upgrade-guide-v3.md``.
    """

    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        warnings.warn(
            "QueryBasedTransformer is deprecated; use the connector-side asset-mapper "
            "pattern (typed records → map_<entity>() → pyatlan_v9 Asset) instead — "
            "will be removed in v4.0. See docs/upgrade-guide-v3.md.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: dict[str, str] = (
            get_yaml_query_template_path_mappings(
                assets=[
                    "TABLE",
                    "COLUMN",
                    "DATABASE",
                    "SCHEMA",
                    "EXTRAS-PROCEDURE",
                    "FUNCTION",
                ]
            )
        )

    def quote_column_name(self, column_name: str) -> str:
        """Handle column names that contain dots by quoting them.

        Args:
            column_name: The column name to process

        Returns:
            The processed column name, quoted if it contains dots
        """
        if "." in column_name:
            return f'"{column_name}"'
        return column_name

    def convert_to_sql_expression(
        self, column: dict[str, str], is_literal: bool = False
    ) -> str:
        """Process a single column definition into a SQL column expression.

        Args:
            column: The column definition dictionary

        Returns:
            A SQL column expression string
        """
        column["name"] = self.quote_column_name(column["name"])
        if is_literal:
            return f"{column['name']} AS {column['name']}"
        return f"{column['source_query']} AS {column['name']}"

    def get_sql_column_expressions(
        self,
        sql_template: dict[str, Any],
        dataframe: pa.Table,
        default_attributes: dict[str, Any],
    ) -> tuple[list[str], list[dict[str, str]] | None]:
        """Get the columns and literal columns for the SQL query.

        Args:
            sql_template (Dict[str, Any]): The SQL template
            dataframe (pa.Table): The Table to get columns from
            default_attributes (Dict[str, Any]): The default attributes to add to the SQL query

        Returns:
            A list of column expressions for the SQL query
        """
        columns: list[str] = []
        literal_columns: list[dict[str, str]] = []
        column_names = list(dataframe.schema.names) + list(default_attributes.keys())

        for column in sql_template["columns"]:
            if (
                column.get("source_columns")
                and (all(col in column_names for col in column["source_columns"]))
                or column["source_query"] in column_names
            ):
                columns.append(self.convert_to_sql_expression(column))

            elif (
                isinstance(column["source_query"], float)
                or isinstance(column["source_query"], int)
                or isinstance(column["source_query"], bool)
                or column["source_query"] is None
            ) or (
                isinstance(column["source_query"], str)
                and column["source_query"].startswith("'")
                and column["source_query"].endswith("'")
                and len(column["source_query"]) > 1
            ):
                literal_columns.append(column)
                columns.append(self.convert_to_sql_expression(column, is_literal=True))

        return columns, literal_columns or None

    def generate_sql_query(
        self,
        yaml_path: str,
        dataframe: pa.Table,
        default_attributes: dict[str, Any],
    ) -> tuple[str, list[dict[str, str]] | None]:
        """
        Generate a SQL query from a YAML template and a DataFrame.

        Args:
            yaml_path (str): The path to the YAML template
            dataframe (pa.Table): The Table to reference for column names
            default_attributes (Dict[str, Any]): The default attributes to add to the SQL query

        Returns:
            str: The generated SQL query
        """
        with open(yaml_path, "r") as f:
            sql_template = yaml.safe_load(f)

        sql_template["columns"] = flatten_yaml_columns(sql_template["columns"])

        columns, literal_columns = self.get_sql_column_expressions(
            sql_template, dataframe, default_attributes
        )

        sql_query = textwrap.dedent(f"""
        SELECT
            {",".join(columns)}
        FROM dataframe
        """)
        return sql_query, literal_columns or None

    def _build_struct(self, level: dict, prefix: str = "") -> None:  # type: ignore[return]
        """No-op shim — struct building moved to get_grouped_dataframe_by_prefix.

        Kept as a no-op so callers that were patching this in tests do not blow up.
        (The enclosing class is itself deprecated; see the class notice.)

        Args:
            level (dict): The current level of the struct hierarchy
            prefix (str): The prefix for the current struct level
        """
        if level is None:
            raise BuildStructLevelRequiredError()
        if prefix is None:
            raise BuildStructPrefixRequiredError()

    def get_grouped_dataframe_by_prefix(self, table: pa.Table) -> list[dict[str, Any]]:
        """Convert flat dot-notation columns to nested dicts.

        We have a flat structured table with columns that have dot notation.
        For example columns like ``attributes.name``, ``attributes.qualifiedName``
        are converted into nested dicts:
        ``{"attributes": {"name": ..., "qualifiedName": ...}}``.

        Args:
            table (pa.Table): Table with flat dot-notation column names

        Returns:
            list[dict[str, Any]]: List of nested dicts
        """

        def collapse_all_none(value: Any) -> Any:
            """Collapse a dict where every (recursive) leaf is None into None itself."""
            if not isinstance(value, dict):
                return value
            collapsed = {k: collapse_all_none(v) for k, v in value.items()}
            return None if all(v is None for v in collapsed.values()) else collapsed

        result = []
        for row_dict in table.to_pylist():
            nested: dict[str, Any] = {}
            for key, value in row_dict.items():
                parts = key.split(".")
                current = nested
                for part in parts[:-1]:
                    child = current.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        current[part] = child
                    current = child
                final_key = parts[-1]
                if not isinstance(current.get(final_key), dict):
                    current[final_key] = value
            result.append({k: collapse_all_none(v) for k, v in nested.items()})
        return result

    def prepare_template_and_attributes(
        self,
        dataframe: pa.Table,
        workflow_id: str,
        workflow_run_id: str,
        connection_qualified_name: str | None = None,
        connection_name: str | None = None,
        entity_sql_template_path: str | None = None,
    ) -> tuple[pa.Table, str]:
        """
        Prepare the entity SQL template and the default attributes for the Table.

        Args:
            dataframe (pa.Table): Input Table
            workflow_id (str): ID of the workflow
            workflow_run_id (str): ID of the workflow run
            connection_qualified_name (str): Qualified name of the connection
            connection_name (str): Name of the connection
            entity_sql_template_path (str): Path to the SQL template

        Returns:
            Tuple[pa.Table, str]: Table with default attributes added and the entity SQL template
        """
        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

        # prepare default attributes as scalar values
        default_attributes: dict[str, Any] = {
            "connection_qualified_name": connection_qualified_name,
            "connection_name": connection_name,
            "tenant_id": self.tenant_id,
            "last_sync_workflow_name": workflow_id,
            "last_sync_run": workflow_run_id,
            "last_sync_run_at": int(datetime.now(UTC).timestamp() * 1000),
            "connector_name": AtlanConnectorType.get_connector_name(
                connection_qualified_name
            ),
        }
        entity_sql_template, literal_columns = self.generate_sql_query(
            entity_sql_template_path, dataframe, default_attributes=default_attributes
        )

        # Add literal columns to default_attributes
        default_attributes.update(
            {
                column["name"].strip('"').strip("'"): (
                    column["source_query"].strip("'")
                    if isinstance(column["source_query"], str)
                    else column["source_query"]
                )
                for column in literal_columns or []
            }
        )

        # Append default attribute columns as constant arrays to the table
        n = len(dataframe)
        for col_name, value in default_attributes.items():
            if col_name not in dataframe.schema.names:
                dataframe = dataframe.append_column(col_name, pa.array([value] * n))

        return dataframe, entity_sql_template

    def transform_metadata(  # type: ignore
        self,
        typename: str,
        dataframe: pa.Table | list[dict[str, Any]],
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: dict[str, type[Any]] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]] | None:
        """Transform records using SQL executed through DuckDB"""
        import pyarrow as pa  # noqa: PLC0415 — optional dep: pyarrow

        if isinstance(dataframe, list):
            dataframe = pa.Table.from_pylist(dataframe) if dataframe else None
        if dataframe is None or len(dataframe) == 0:
            return None

        # Load the YAML template for the given typename
        typename = typename.upper()
        self.entity_class_definitions = (
            entity_class_definitions or self.entity_class_definitions
        )
        entity_sql_template_path = self.entity_class_definitions.get(typename)
        if not entity_sql_template_path:
            raise SqlTransformNotRegisteredError(
                message=f"No SQL transformation registered for {typename}"
            )

        # prepare the SQL to run on the dataframe and the default attributes
        dataframe, entity_sql_template = self.prepare_template_and_attributes(
            dataframe,
            workflow_id,
            workflow_run_id,
            connection_qualified_name=kwargs.get("connection_qualified_name"),
            connection_name=kwargs.get("connection_name"),
            entity_sql_template_path=entity_sql_template_path,
        )

        # run the SQL on the table via DuckDB
        from application_sdk.common.incremental.storage.duckdb_utils import (  # noqa: PLC0415
            DuckDBConnectionManager,
        )

        logger.debug(
            "Running transformer for asset typename=%s sql=%s",
            typename,
            entity_sql_template,
        )
        with DuckDBConnectionManager() as db:
            db.connection.register("dataframe", dataframe)
            result_table = db.connection.execute(entity_sql_template).to_arrow_table()

        # Convert flat dot-notation columns into nested dicts
        return self.get_grouped_dataframe_by_prefix(result_table)
