import textwrap
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Type

import daft
import yaml
from pyatlan.model.enums import AtlanConnectorType

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers import TransformerInterface
from application_sdk.transformers.common.utils import (
    flatten_yaml_columns,
    get_yaml_query_template_path_mappings,
)

logger = get_logger(__name__)


class QueryBasedTransformer(TransformerInterface):
    """Query based transformer that uses YAML files for SQL queries and daft engine for execution.

    Uses a YAML file to define SQL queries for each asset type and executes them on raw dataframes
    using the daft engine to get transformed data.

    The execution flow is:
        1. Initialize transformer with connector name and tenant ID
        2. Map asset types (DATABASE, SCHEMA, TABLE, COLUMN etc) to YAML template paths
           from default or custom template directories
        3. Transform metadata by:
           - Loading YAML template for the typename
           - Preparing default attributes and SQL template
           - Generating SQL query from template
           - Executing query on raw daft dataframe
           - Converting flat dataframe with dot notation to nested structure
           - Returning transformed dataframe

    Args:
        connector_name: Name of the connector
        tenant_id: ID of the tenant
        **kwargs: Additional keyword arguments
    """

    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: Dict[str, str] = (
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
        self, column: Dict[str, str], is_literal: bool = False
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
        sql_template: Dict[str, Any],
        dataframe: daft.DataFrame,
        default_attributes: Dict[str, Any],
    ) -> Tuple[List[str], Optional[List[Dict[str, str]]]]:
        """Get the columns and literal columns for the SQL query.

        Args:
            sql_template (Dict[str, Any]): The SQL template
            dataframe (daft.DataFrame): The DataFrame to get columns from
            default_attributes (Dict[str, Any]): The default attributes to add to the SQL query

        Returns:
            A list of column expressions for the SQL query
        """
        columns: List[str] = []
        literal_columns: List[Dict[str, str]] = []
        column_names = dataframe.column_names + list(default_attributes.keys())

        # Add the columns from the SQL template to the columns list only if they are present in the dataframe
        # Otherwise the dataframe will throw an error
        for column in sql_template["columns"]:
            # If the column has a source_columns attribute and all of the source_columns are present in the dataframe,
            # then add the column to the columns list
            # E.g
            # - name: attributes.qualifiedName
            #   source_query: concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name)
            #   source_columns: [connection_qualified_name, table_catalog, table_schema, table_name]
            if column.get("source_columns") and (
                all(col in column_names for col in column["source_columns"])
            ):
                columns.append(self.convert_to_sql_expression(column))

            # Else if the column has a source_query attribute and the source_query is present in the dataframe,
            # then add the column to the columns list
            # E.g
            # - name: attributes.tableName
            #   source_query: table_name
            elif column["source_query"] in column_names:
                columns.append(self.convert_to_sql_expression(column))

            # Else if the column has a string literal, then add the column to the literal_columns list
            # E.g 1. String Literal
            # - name: attributes.typeName
            #   source_query: "'Table'"

            # E.g 2. Boolean Literal
            # - name: attributes.isPartition
            #   source_query: True
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
        dataframe: daft.DataFrame,
        default_attributes: Dict[str, Any],
    ) -> Tuple[str, Optional[List[Dict[str, str]]]]:
        """
        Generate a SQL query from a YAML template and a DataFrame.

        Args:
            yaml_path (str): The path to the YAML template
            dataframe (daft.DataFrame): The DataFrame to reference for column names
            default_attributes (Dict[str, Any]): The default attributes to add to the SQL query

        Returns:
            str: The generated SQL query
        """
        try:
            # Load the YAML template from the path
            with open(yaml_path, "r") as f:
                sql_template = yaml.safe_load(f)

            # Flatten the columns dictionary
            sql_template["columns"] = flatten_yaml_columns(sql_template["columns"])

            # Get the SQL columns expressions for the SQL query
            columns, literal_columns = self.get_sql_column_expressions(
                sql_template, dataframe, default_attributes
            )

            # Join all the SQL column expressions to create the full SELECT statement for the SQL query
            # This will be used for transforming the dataframe
            sql_query = textwrap.dedent(f"""
            SELECT
                {','.join(columns)}
            FROM dataframe
            """)
            return sql_query, literal_columns or None
        except Exception as e:
            logger.error(f"Error generating query: {e}")
            raise e

    def _build_struct(self, level: dict, prefix: str = "") -> Optional[daft.Expression]:
        """
        Recursively build nested struct expressions.

        Args:
            level (dict): The current level of the struct hierarchy
            prefix (str): The prefix for the current struct level

        Returns:
            Optional[daft.Expression]: The constructed struct expression or None if all fields are null
        """
        struct_fields = []
        non_null_fields = []

        # Handle columns at this level
        if "columns" in level:
            for full_col, suffix in level["columns"]:
                field = daft.col(full_col).alias(suffix)
                struct_fields.append(field)
                # Add to non_null check by negating is_null()
                non_null_fields.append(~daft.col(full_col).is_null())

        # Handle nested levels
        for component, sub_level in level.items():
            if component != "columns":  # Skip the columns key
                nested_struct = self._build_struct(sub_level, component)
                if nested_struct is not None:
                    struct_fields.append(nested_struct)
                    # Add nested struct's non-null check
                    non_null_fields.append(~nested_struct.is_null())

        # Only create a struct if we have fields
        if struct_fields:
            # Create the struct first
            struct = daft.struct(*struct_fields)

            # If we have non-null checks, apply them
            if non_null_fields:
                # Combine all non-null checks with OR to check if any field is non-null
                any_non_null = non_null_fields[0]
                for check in non_null_fields[1:]:
                    any_non_null = any_non_null | check

                # Use if_else on the any_non_null Expression
                return any_non_null.if_else(struct, None).alias(prefix)

            return struct.alias(prefix)

        return None

    def get_grouped_dataframe_by_prefix(
        self, dataframe: daft.DataFrame
    ) -> daft.DataFrame:
        """Group columns with the same prefix into structs, supporting any level of nesting.

        We have a flat structured dataframe with columns that have dot notation in the yaml template.
        For example:

        .. code-block:: yaml

            - name: attributes.name
              source_query: table_name
            - name: attributes.qualifiedName
              source_query: concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name)
              source_columns: [connection_qualified_name, table_catalog, table_schema, table_name]
            - name: attributes.connectionQualifiedName
              source_query: connection_qualified_name

        This method will group the columns with the same prefix into structs.
        For example:

        .. code-block:: python

            struct(
                name=table_name,
                qualifiedName=concat(connection_qualified_name, '/', table_catalog, '/', table_schema, '/', table_name),
                connectionQualifiedName=connection_qualified_name
            ).alias("attributes")

        Args:
            dataframe (daft.DataFrame): DataFrame to restructure

        Returns:
            daft.DataFrame: DataFrame with columns grouped into structs
        """
        try:
            # Get all column names
            columns = dataframe.column_names

            # Group columns by their path components
            path_groups = {}
            standalone_columns = []

            for col in columns:
                if "." in col:
                    # Split the full path into components
                    path_components = col.split(".")
                    current_level = path_groups

                    # Traverse the path, creating nested dictionaries as needed
                    for component in path_components[:-1]:
                        if component not in current_level:
                            current_level[component] = {}
                        current_level = current_level[component]

                    # Store the column name and its final component at the leaf level
                    if "columns" not in current_level:
                        current_level["columns"] = []
                    current_level["columns"].append((col, path_components[-1]))
                else:
                    standalone_columns.append(col)

            # Create new DataFrame with restructured columns
            new_columns = []

            # Add standalone columns as is
            for col in standalone_columns:
                new_columns.append(daft.col(col))

            # Build nested structs starting from the root level
            for prefix, level in path_groups.items():
                struct_expr = self._build_struct(level, prefix)
                new_columns.append(struct_expr)

            return dataframe.select(*new_columns)
        except Exception as e:
            logger.error(f"Error grouping columns by prefix: {e}")
            raise e

    def prepare_template_and_attributes(
        self,
        dataframe: daft.DataFrame,
        workflow_id: str,
        workflow_run_id: str,
        connection_qualified_name: Optional[str] = None,
        connection_name: Optional[str] = None,
        entity_sql_template_path: Optional[str] = None,
    ) -> Tuple[daft.DataFrame, str]:
        """
        Prepare the entity SQL template and the default attributes for the DataFrame.

        Args:
            dataframe (daft.DataFrame): Input DataFrame
            workflow_id (str): ID of the workflow
            workflow_run_id (str): ID of the workflow run
            connection_qualified_name (str): Qualified name of the connection
            connection_name (str): Name of the connection

        Returns:
            Tuple[daft.DataFrame, str]: DataFrame with default attributes added and the entity SQL template
        """
        # prepare default attributes
        default_attributes = {
            "connection_qualified_name": daft.lit(connection_qualified_name),
            "connection_name": daft.lit(connection_name),
            "tenant_id": daft.lit(self.tenant_id),
            "last_sync_workflow_name": daft.lit(workflow_id),
            "last_sync_run": daft.lit(workflow_run_id),
            "last_sync_run_at": daft.lit(
                int(datetime.now(timezone.utc).timestamp() * 1000)
            ),
            "connector_name": daft.lit(
                AtlanConnectorType.get_connector_name(connection_qualified_name)
            ),
        }
        entity_sql_template, literal_columns = self.generate_sql_query(
            entity_sql_template_path, dataframe, default_attributes=default_attributes
        )

        # We have to prepare the literal attributes in the raw dataframe because
        # we get an error which is due to the mismatch in lengths between the
        # literal values and the columns in the DataFrame.
        # The daft.lit function creates a literal value that is not automatically broadcasted
        # to match the length of the DataFrame columns.
        # This results in a length mismatch when constructing the struct.
        default_attributes.update(
            {
                column["name"].strip('"').strip("'"): daft.lit(
                    column["source_query"].strip("'")
                    if isinstance(column["source_query"], str)
                    else column["source_query"]
                )
                for column in literal_columns or []
            }
        )

        return dataframe.with_columns(default_attributes), entity_sql_template

    def transform_metadata(  # type: ignore
        self,
        typename: str,
        dataframe: daft.DataFrame,
        workflow_id: str,
        workflow_run_id: str,
        entity_class_definitions: Dict[str, Type[Any]] | None = None,
        **kwargs: Any,
    ) -> Optional[daft.DataFrame]:
        """Transform records using SQL executed through Daft"""
        try:
            if dataframe.count_rows() == 0:
                return None

            # Load the YAML template for the given typename
            typename = typename.upper()
            self.entity_class_definitions = (
                entity_class_definitions or self.entity_class_definitions
            )
            entity_sql_template_path = self.entity_class_definitions.get(typename)
            if not entity_sql_template_path:
                raise ValueError(f"No SQL transformation registered for {typename}")

            # prepare the SQL to run on the dataframe and the default attributes
            dataframe, entity_sql_template = self.prepare_template_and_attributes(
                dataframe,
                workflow_id,
                workflow_run_id,
                connection_qualified_name=kwargs.get("connection_qualified_name"),
                connection_name=kwargs.get("connection_name"),
                entity_sql_template_path=entity_sql_template_path,
            )

            # run the SQL on the dataframe
            logger.info(
                f"Running transformer for asset [{typename}] with SQL:\n {entity_sql_template}"
            )
            transformed_df = daft.sql(entity_sql_template)

            # We have a flat structured dataframe with columns that have dot notation
            # for their path. We want to group the columns with the same prefix into structs.
            return self.get_grouped_dataframe_by_prefix(transformed_df)
        except Exception as e:
            logger.error(f"Error transforming {typename}: {e}")
            raise e
