import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

import daft

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.transformers import TransformerInterface

logger = get_logger(__name__)

import yaml


class SQLTransformer(TransformerInterface):
    def __init__(self, connector_name: str, tenant_id: str, **kwargs: Any):
        self.connector_name = connector_name
        self.tenant_id = tenant_id
        self.entity_class_definitions: Dict[str, str] = (
            self._generate_detault_yaml_mappings(entities=["DATABASE", "SCHEMA", "TABLE", "COLUMN"])
        )

    def _generate_detault_yaml_mappings(
        self,
        entities: List[str],
        base_path: str = f"{os.path.dirname(__file__)}/sql_query_templates",
    ) -> Dict[str, str]:
        return {
            entity: os.path.join(base_path, f"{entity.lower()}.yaml")
            for entity in entities
        }

    def _process_column_name(self, column_name: str) -> str:
        """Handle column names that contain dots by quoting them.

        Args:
            column_name: The column name to process

        Returns:
            The processed column name, quoted if it contains dots
        """
        if "." in column_name:
            return f'"{column_name}"'
        return column_name

    def _process_column(self, column: Dict[str, str]) -> str:
        """Process a single column definition into a SQL column expression.

        Args:
            column: The column definition dictionary

        Returns:
            A SQL column expression string
        """
        column["name"] = self._process_column_name(column["name"])
        return f"{column['source_query']} AS {column['name']}"

    def _get_columns(
        self, sql_template: Dict[str, Any], dataframe: daft.DataFrame
    ) -> List[str]:
        """Get the columns for the SQL query.

        Args:
            sql_template (Dict[str, Any]): The SQL template
            dataframe (daft.DataFrame): The DataFrame to get columns from

        Returns:
            A list of column expressions for the SQL query
        """
        columns: List[Dict[str, str]] = []
        for column in sql_template["columns"]:
            if column.get("source_columns") and all(
                col in dataframe.column_names for col in column["source_columns"]
            ):
                columns.append(self._process_column(column))
            elif column["source_query"] in dataframe.column_names:
                columns.append(self._process_column(column))
            elif (
                column["source_query"].startswith("'")
                and column["source_query"].endswith("'")
                and len(column["source_query"]) > 1
            ):
                # This is a string literal and should be added as is
                columns.append(self._process_column(column))

        return columns

    def generate_sql_query(self, yaml_path: str, dataframe: daft.DataFrame) -> str:
        """
        Generate a SQL query from a YAML template and a DataFrame.

        Args:
            yaml_path (str): The path to the YAML template
            dataframe (daft.DataFrame): The DataFrame to reference for column names

        Returns:
            str: The generated SQL query
        """
        try:
            with open(yaml_path, "r") as f:
                sql_template = yaml.safe_load(f)
            columns = self._get_columns(sql_template, dataframe)

            return f"""
            SELECT
                {','.join(columns)}
            FROM dataframe
            """
        except Exception as e:
            logger.error(f"Error generating query: {e}")
            raise e
    
    def _build_struct(self, level: dict, prefix: str = "") -> daft.Expression:
        """Recursively build nested struct expressions."""
        struct_fields = []
        
        # Handle columns at this level
        if "columns" in level:
            for full_col, suffix in level["columns"]:
                struct_fields.append(daft.col(full_col).alias(suffix))
        
        # Handle nested levels
        for component, sub_level in level.items():
            if component != "columns":  # Skip the columns key
                nested_struct = self._build_struct(sub_level, component)
                struct_fields.append(nested_struct)
        
        # Only create a struct if we have fields
        if struct_fields:
            return daft.struct(*struct_fields).alias(prefix)
        return None

    def _group_columns_by_prefix(self, dataframe: daft.DataFrame) -> daft.DataFrame:
        """Group columns with the same prefix into structs, supporting any level of nesting.

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
                if struct_expr is not None:
                    new_columns.append(struct_expr)

            return dataframe.select(*new_columns)
        except Exception as e:
            logger.error(f"Error grouping columns by prefix: {e}")
            raise e
    
    def _prepare_default_attributes(
        self,
        dataframe: daft.DataFrame,
        workflow_id: str,
        workflow_run_id: str,
        connection_qualified_name: Optional[str] = None,
        connection_name: Optional[str] = None,
    ) -> daft.DataFrame:
        """
        Prepare default attributes for the DataFrame.

        Args:
            dataframe (daft.DataFrame): Input DataFrame
            workflow_id (str): ID of the workflow
            workflow_run_id (str): ID of the workflow run
            connection_qualified_name (str): Qualified name of the connection
            connection_name (str): Name of the connection

        Returns:
            daft.DataFrame: DataFrame with default attributes added
        """
        current_utc_time = datetime.now(timezone.utc)
        return dataframe.with_columns(
            {
                "connection_qualified_name": daft.lit(connection_qualified_name),
                "connection_name": daft.lit(connection_name),
                "tenant_id": daft.lit(self.tenant_id),
                "last_sync_workflow_name": daft.lit(workflow_id),
                "last_sync_run": daft.lit(workflow_run_id),
                "last_sync_run_at": daft.lit(current_utc_time),
                "connector_name": daft.lit(self.connector_name),
            }
        )

    def transform_metadata(
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

            typename = typename.upper()
            self.entity_class_definitions = (
                entity_class_definitions or self.entity_class_definitions
            )
            entity_sql_template_path = self.entity_class_definitions.get(typename)
            if not entity_sql_template_path:
                raise ValueError(f"No SQL transformation registered for {typename}")
            
            dataframe = self._prepare_default_attributes(
                dataframe, workflow_id, workflow_run_id, 
                connection_qualified_name=kwargs.get("connection_qualified_name"),
                connection_name=kwargs.get("connection_name"),
            )

            entity_sql_template = self.generate_sql_query(
                entity_sql_template_path, dataframe
            )
            transformed_df = daft.sql(entity_sql_template)
            transformed_df = self._group_columns_by_prefix(transformed_df)
            return transformed_df
        except Exception as e:
            logger.error(f"Error transforming {typename}: {e}")
            raise e
