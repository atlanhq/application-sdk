"""SQL entity transformers for Atlas.

This module provides classes for transforming SQL metadata into Atlas entities,
including databases, schemas, tables, columns, functions, and tag attachments.
"""

import json
from typing import Any, Dict, Optional, Set, TypeVar, overload

from pyatlan.model import assets
from pyatlan.model.enums import AtlanConnectorType
from pyatlan.utils import init_guid, validate_required_fields

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers.common.utils import build_atlas_qualified_name

logger = get_logger(__name__)

T = TypeVar("T")


class Procedure(assets.Procedure):
    """Procedure entity transformer for Atlas.

    This class handles the transformation of database stored procedure metadata
    into Atlas-compatible entity format. It validates required fields and
    constructs qualified names and attributes according to Atlas specifications.

    Attributes:
        name (str): Name of the procedure.
        qualified_name (str): Fully qualified name of the procedure.
        schema_qualified_name (str): Qualified name of the schema containing the procedure.
        schema_name (str): Name of the schema containing the procedure.
        database_name (str): Name of the database containing the procedure.
        database_qualified_name (str): Qualified name of the database.
        connection_qualified_name (str): Qualified name of the connection.
        definition (str): Source code or definition of the procedure.
        sub_type (str): Subtype or category of the procedure.
    """

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a Procedure entity's attributes.

        This method validates required fields and constructs the procedure's
        attributes including qualified names, schema references, and metadata.

        Args:
            obj (Dict[str, Any]): Dictionary containing procedure metadata with fields:
                - procedure_name (str): Name of the procedure
                - procedure_definition (str): Source code of the procedure
                - procedure_catalog (str): Database/catalog containing the procedure
                - procedure_schema (str): Schema containing the procedure
                - connection_qualified_name (str): Connection identifier
                - procedure_type (str, optional): Type/category of procedure

        Returns:
            Dict[str, Any]: Dictionary containing:
                - attributes (Dict[str, Any]): Procedure attributes formatted for Atlas
                - custom_attributes (Dict[str, Any]): Custom metadata attributes
                - entity_class (Type): The Procedure class type

        Raises:
            ValueError: If any required fields are missing or None.
        """
        try:
            assert (
                obj.get("procedure_name") is not None
            ), "Procedure name cannot be None"
            assert (
                obj.get("procedure_definition") is not None
            ), "Procedure definition cannot be None"
            assert (
                obj.get("procedure_catalog") is not None
            ), "Procedure catalog cannot be None"
            assert (
                obj.get("procedure_schema") is not None
            ), "Procedure schema cannot be None"
            assert (
                obj.get("connection_qualified_name") is not None
            ), "Connection qualified name cannot be None"

            procedure_attributes = {}
            procedure_custom_attributes = {}

            procedure_attributes["qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["procedure_catalog"],
                obj["procedure_schema"],
                "_procedures_",
                obj["procedure_name"],
            )
            procedure_attributes["name"] = obj["procedure_name"]
            procedure_attributes["definition"] = obj["procedure_definition"]
            procedure_attributes["schema_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["procedure_catalog"],
                obj["procedure_schema"],
            )
            procedure_attributes["database_qualified_name"] = (
                build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["procedure_catalog"],
                )
            )
            procedure_attributes["schema_name"] = obj["procedure_schema"]
            procedure_attributes["database_name"] = obj["procedure_catalog"]
            procedure_attributes["connection_qualified_name"] = obj[
                "connection_qualified_name"
            ]
            procedure_attributes["sub_type"] = obj.get("procedure_type", "-1")
            procedure_attributes["atlanSchema"] = {
                "typeName": "Schema",
                "uniqueAttributes": {
                    "qualifiedName": procedure_attributes["schema_qualified_name"]
                },
            }

            return {
                "attributes": procedure_attributes,
                "custom_attributes": procedure_custom_attributes,
                "entity_class": Procedure,
            }
        except AssertionError as e:
            raise ValueError(f"Error creating Procedure Entity: {str(e)}")


class Database(assets.Database):
    """Database entity transformer for Atlas.

    This class handles the transformation of database metadata into Atlas Database entities.
    """

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a Database entity.

        Args:
            obj (Dict[str, Any]): Dictionary containing database metadata.

        Returns:
            assets.Database: The created Database entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        try:
            assert obj.get("database_name") is not None and isinstance(
                obj.get("database_name"), str
            ), "Database name cannot be None"
            assert obj.get("connection_qualified_name") is not None and isinstance(
                obj.get("connection_qualified_name"), str
            ), "Connection qualified name cannot be None"

            database_attributes = {}
            database_custom_attributes = {}

            database_attributes["qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"], obj["database_name"]
            )

            database_attributes["name"] = obj["database_name"]
            database_attributes["connection_qualified_name"] = obj[
                "connection_qualified_name"
            ]
            database_attributes["schema_count"] = obj.get("schema_count", 0)

            if catalog_id := obj.get("catalog_id", None):
                database_custom_attributes["catalog_id"] = catalog_id

            return {
                "attributes": database_attributes,
                "custom_attributes": database_custom_attributes,
                "entity_class": Database,
            }
        except AssertionError as e:
            raise ValueError(f"Error creating Database Entity: {str(e)}")


class Schema(assets.Schema):
    """Schema entity transformer for Atlas.

    This class handles the transformation of schema metadata into Atlas Schema entities.
    """

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a Schema entity.

        Args:
            obj (Dict[str, Any]): Dictionary containing schema metadata.

        Returns:
            assets.Schema: The created Schema entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        try:
            assert obj.get("schema_name") is not None and isinstance(
                obj.get("schema_name"), str
            ), "Schema name cannot be None"
            assert obj.get("connection_qualified_name") is not None and isinstance(
                obj.get("connection_qualified_name"), str
            ), "Connection qualified name cannot be None"

            schema_attributes = {}
            schema_custom_attributes = {}

            schema_attributes["qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["catalog_name"],
                obj["schema_name"],
            )

            schema_attributes["name"] = obj["schema_name"]
            schema_attributes["database_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"], obj["catalog_name"]
            )
            schema_attributes["database_name"] = obj["catalog_name"]
            schema_attributes["connection_qualified_name"] = obj[
                "connection_qualified_name"
            ]
            schema_attributes["table_count"] = obj.get("table_count", 0)
            schema_attributes["views_count"] = obj.get("views_count", 0)

            if catalog_id := obj.get("catalog_id", None):
                schema_custom_attributes["catalog_id"] = catalog_id

            if is_managed_access := obj.get("is_managed_access", None):
                schema_custom_attributes["is_managed_access"] = is_managed_access

            schema_attributes["database"] = {
                "typeName": "Database",
                "uniqueAttributes": {
                    "qualifiedName": schema_attributes["database_qualified_name"]
                },
            }

            return {
                "attributes": schema_attributes,
                "custom_attributes": schema_custom_attributes,
                "entity_class": Schema,
            }
        except AssertionError as e:
            raise ValueError(f"Error creating Schema Entity: {str(e)}")


class Table(assets.Table):
    """Table entity transformer for Atlas.

    This class handles the transformation of table metadata into Atlas Table entities,
    including regular tables, views, materialized views, and dynamic tables.
    """

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a Table entity.

        Args:
            obj (Dict[str, Any]): Dictionary containing table metadata.

        Returns:
            Union[assets.Table, assets.View, assets.MaterialisedView, assets.SnowflakeDynamicTable]:
                The created Table entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        try:
            # Needed? Sequences don't have a table_name, table_schema
            assert obj.get("table_name") is not None, "Table name cannot be None"
            assert obj.get("table_schema") is not None, "Table schema cannot be None"

            assert obj.get("table_catalog") is not None, "Table catalog cannot be None"

            # Determine the type of table based on metadata
            is_partition = bool(obj.get("is_partition", False))
            table_type_value = obj.get("table_type", "TABLE")
            is_dynamic = obj.get("is_dynamic") == "YES"

            if is_partition:
                table_type = assets.TablePartition
            elif table_type_value in [
                "TABLE",
                "BASE TABLE",
                "FOREIGN TABLE",
                "PARTITIONED TABLE",
            ]:
                table_type = assets.Table
            elif table_type_value == "MATERIALIZED VIEW":
                table_type = assets.MaterialisedView
            elif table_type_value == "DYNAMIC TABLE" or is_dynamic:
                table_type = assets.SnowflakeDynamicTable
            else:
                table_type = assets.View

            sql_table_attributes = {}
            if table_type == assets.TablePartition:
                sql_table_attributes["qualified_name"] = build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["table_catalog"],
                    obj["table_schema"],
                    obj["table_name"],
                )
                sql_table_attributes["table_qualified_name"] = (
                    build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        obj["table_catalog"],
                        obj["table_schema"],
                        obj["parent_table_name"],
                    )
                )
                sql_table_attributes["name"] = obj["table_name"]
                sql_table_attributes["schema_qualified_name"] = (
                    build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        obj["table_catalog"],
                        obj["table_schema"],
                    )
                )
                sql_table_attributes["schema_name"] = obj["table_schema"]
                sql_table_attributes["database_name"] = obj["table_catalog"]
                sql_table_attributes["database_qualified_name"] = (
                    build_atlas_qualified_name(
                        obj["connection_qualified_name"], obj["table_catalog"]
                    )
                )
                sql_table_attributes["connection_qualified_name"] = obj[
                    "connection_qualified_name"
                ]
                sql_table_attributes["table_name"] = obj["parent_table_name"]
                sql_table_attributes["table_qualified_name"] = (
                    build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        obj["table_catalog"],
                        obj["table_schema"],
                        obj["parent_table_name"],
                    )
                )
                if obj.get("partitioned_parent_table", None):
                    sql_table_attributes["parent_table_partition"] = (
                        assets.TablePartition.ref_by_qualified_name(
                            qualified_name=sql_table_attributes["table_qualified_name"]
                        )
                    )
                else:
                    sql_table_attributes["parent_table"] = Table.ref_by_qualified_name(
                        qualified_name=sql_table_attributes["table_qualified_name"]
                    )
            else:
                sql_table_attributes["name"] = obj["table_name"]
                sql_table_attributes["qualified_name"] = build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["table_catalog"],
                    obj["table_schema"],
                    obj["table_name"],
                )
                sql_table_attributes["schema_qualified_name"] = (
                    build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        obj["table_catalog"],
                        obj["table_schema"],
                    )
                )
                sql_table_attributes["schema_name"] = obj["table_schema"]
                sql_table_attributes["database_name"] = obj["table_catalog"]
                sql_table_attributes["database_qualified_name"] = (
                    build_atlas_qualified_name(
                        obj["connection_qualified_name"], obj["table_catalog"]
                    )
                )
                sql_table_attributes["connection_qualified_name"] = obj[
                    "connection_qualified_name"
                ]

            if table_type in [assets.View, assets.MaterialisedView]:
                sql_table_attributes["definition"] = obj.get("view_definition", "")

            sql_table_attributes["column_count"] = obj.get("column_count", 0)
            sql_table_attributes["row_count"] = obj.get("row_count", 0)
            sql_table_attributes["size_bytes"] = obj.get("size_bytes", 0)

            sql_table_attributes["atlanSchema"] = {
                "typeName": "Schema",
                "uniqueAttributes": {
                    "qualifiedName": sql_table_attributes["schema_qualified_name"]
                },
            }

            table = table_type()
            if hasattr(table, "external_location"):
                sql_table_attributes["external_location"] = obj.get("location", "")

            if hasattr(table, "external_location_format"):
                sql_table_attributes["external_location_format"] = obj.get(
                    "file_format_type", ""
                )

            if hasattr(table, "external_location_region"):
                sql_table_attributes["external_location_region"] = obj.get(
                    "stage_region", ""
                )

            # Applicable only for Materialised Views
            if obj.get("refresh_mode", "") != "":
                sql_table_attributes["refresh_mode"] = obj.get("refresh_mode")

            # Applicable only for Materialised Views
            if obj.get("staleness", "") != "":
                sql_table_attributes["staleness"] = obj.get("staleness")

            # Applicable only for Materialised Views
            if obj.get("stale_since_date", "") != "":
                sql_table_attributes["stale_since_date"] = obj.get("stale_since_date")

            # Applicable only for Materialised Views
            if obj.get("refresh_method", "") != "":
                sql_table_attributes["refresh_method"] = obj.get("refresh_method")

            custom_attributes = {}

            # Applicable only for Materialised Views
            if not table.custom_attributes:
                custom_attributes["table_type"] = table_type_value

            if obj.get("is_transient", "") != "":
                custom_attributes["is_transient"] = obj.get("is_transient")

            if obj.get("table_catalog_id", "") != "":
                custom_attributes["catalog_id"] = obj.get("table_catalog_id")

            if obj.get("table_schema_id", "") != "":
                custom_attributes["schema_id"] = obj.get("table_schema_id")

            if obj.get("last_ddl", "") != "":
                custom_attributes["last_ddl"] = obj.get("last_ddl")
            if obj.get("last_ddl_by", "") != "":
                custom_attributes["last_ddl_by"] = obj.get("last_ddl_by")

            if obj.get("is_secure", "") != "":
                custom_attributes["is_secure"] = obj.get("is_secure")

            if obj.get("retention_time", "") != "":
                custom_attributes["retention_time"] = obj.get("retention_time")

            if obj.get("stage_url", "") != "":
                custom_attributes["stage_url"] = obj.get("stage_url")

            if obj.get("is_insertable_into", "") != "":
                custom_attributes["is_insertable_into"] = obj.get("is_insertable_into")

            if obj.get("number_columns_in_part_key", "") != "":
                custom_attributes["number_columns_in_part_key"] = obj.get(
                    "number_columns_in_part_key"
                )
            if obj.get("columns_participating_in_part_key", "") != "":
                custom_attributes["columns_participating_in_part_key"] = obj.get(
                    "columns_participating_in_part_key"
                )
            if obj.get("is_typed", "") != "":
                custom_attributes["is_typed"] = obj.get("is_typed")

            if obj.get("auto_clustering_on", "") != "":
                custom_attributes["auto_clustering_on"] = obj.get("auto_clustering_on")

            custom_attributes["engine"] = obj.get("engine")

            if obj.get("auto_increment", "") != "":
                custom_attributes["auto_increment"] = obj.get("auto_increment")

            return {
                "attributes": sql_table_attributes,
                "custom_attributes": custom_attributes,
                "entity_class": table_type,
            }
        except AssertionError as e:
            raise ValueError(f"Error creating Table Entity: {str(e)}")


class Column(assets.Column):
    """Column entity transformer for Atlas.

    This class handles the transformation of column metadata into Atlas Column entities.
    """

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a Column entity.

        Args:
            obj (Dict[str, Any]): Dictionary containing column metadata.

        Returns:
            assets.Column: The created Column entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        try:
            assert obj.get("column_name") is not None, "Column name cannot be None"
            assert obj.get("table_catalog") is not None, "Table catalog cannot be None"
            assert obj.get("table_schema") is not None, "Table schema cannot be None"
            assert obj.get("table_name") is not None, "Table name cannot be None"
            assert (
                obj.get("ordinal_position") is not None
            ), "Ordinal position cannot be None"
            assert obj.get("data_type") is not None, "Data type cannot be None"

            attributes = {}
            parent_type = None
            table_qualified_name = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["table_catalog"],
                obj["table_schema"],
                obj["table_name"],
            )
            if obj.get("table_type") in ["VIEW"]:
                parent_type = assets.View
                attributes["view"] = {
                    "typeName": "View",
                    "uniqueAttributes": {"qualifiedName": table_qualified_name},
                }
                attributes["view_name"] = obj["table_name"]
                attributes["view_qualified_name"] = table_qualified_name
            elif obj.get("table_type") in ["MATERIALIZED VIEW"]:
                parent_type = assets.MaterialisedView
                attributes["materialisedView"] = {
                    "typeName": "MaterialisedView",
                    "uniqueAttributes": {
                        "qualifiedName": table_qualified_name,
                    },
                }
                attributes["view_name"] = obj["table_name"]
                attributes["view_qualified_name"] = table_qualified_name
            elif (
                obj.get("table_type") in ["DYNAMIC TABLE"]
                or obj.get("is_dynamic") == "YES"
            ):
                parent_type = assets.SnowflakeDynamicTable
                attributes["dynamicTable"] = {
                    "typeName": "SnowflakeDynamicTable",
                    "uniqueAttributes": {"qualifiedName": table_qualified_name},
                }
                attributes["table_name"] = obj["table_name"]
                attributes["table_qualified_name"] = table_qualified_name
            elif obj.get("belongs_to_partition") == "YES":
                parent_type = assets.TablePartition
                attributes["tablePartition"] = {
                    "typeName": "TablePartition",
                    "uniqueAttributes": {"qualifiedName": table_qualified_name},
                }
                attributes["table_name"] = obj["table_name"]
                attributes["table_qualified_name"] = table_qualified_name
            elif obj.get("table_type") in [
                "TABLE",
                "BASE TABLE",
                "FOREIGN TABLE",
                "PARTITIONED TABLE",
            ]:
                parent_type = assets.Table
                attributes["table"] = {
                    "typeName": "Table",
                    "uniqueAttributes": {"qualifiedName": table_qualified_name},
                }
                attributes["table_name"] = obj["table_name"]
                attributes["table_qualified_name"] = table_qualified_name
            else:
                parent_type = assets.View
                attributes["view"] = {
                    "typeName": "View",
                    "uniqueAttributes": {"qualifiedName": table_qualified_name},
                }
                attributes["table_name"] = obj["table_name"]
                attributes["table_qualified_name"] = table_qualified_name
            attributes["name"] = obj.get("column_name")
            attributes["qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["table_catalog"],
                obj["table_schema"],
                obj["table_name"],
                obj["column_name"],
            )
            attributes["parent_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["table_catalog"],
                obj["table_schema"],
                obj["table_name"],
            )
            attributes["parent_type"] = parent_type
            attributes["order"] = obj.get(
                "ordinal_position",
                obj.get("column_id", obj.get("internal_column_id", None)),
            )
            attributes["parent_name"] = obj["table_name"]
            attributes["database_name"] = obj["table_catalog"]
            attributes["database_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"], obj["table_catalog"]
            )
            attributes["schema_name"] = obj["table_schema"]
            attributes["schema_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["table_catalog"],
                obj["table_schema"],
            )
            attributes["connection_qualified_name"] = obj["connection_qualified_name"]
            attributes["data_type"] = obj.get("data_type")
            attributes["is_nullable"] = obj.get("is_nullable", "YES") == "YES"
            attributes["is_partition"] = obj.get("is_partition", None) == "YES"
            attributes["partition_order"] = obj.get("partition_order", 0)
            attributes["is_primary"] = obj.get("primary_key", None) == "YES"
            attributes["is_foreign"] = obj.get("foreign_key", None) == "YES"
            attributes["max_length"] = obj.get("character_maximum_length", 0)
            attributes["numeric_scale"] = obj.get("numeric_scale", 0)

            if obj.get("decimal_digits", "") != "":
                attributes["precision"] = obj.get("decimal_digits")

            optional_custom_attributes_key = [
                "numeric_precision",
                "character_octet_length",
                "is_auto_increment",
                "is_generated",
                "num_prec_radix" "extra_info",
                "buffer_length",
                "column_size",
            ]

            custom_attributes = {
                "ordinal_position": obj.get("ordinal_position"),
                "is_self_referencing": obj.get("is_self_referencing", "NO"),
                "type_name": obj.get("type_name", obj.get("data_type")),
            }

            for key in optional_custom_attributes_key:
                if obj.get(key):
                    custom_attributes[key] = obj.get(key)

            return {
                "attributes": attributes,
                "custom_attributes": custom_attributes,
                "entity_class": Column,
            }
        except AssertionError as e:
            raise ValueError(f"Error creating Column Entity: {str(e)}")


class Function(assets.Function):
    """Function entity transformer for Atlas.

    This class handles the transformation of database function metadata into
    Atlas-compatible entity format. It validates required fields and constructs
    qualified names and attributes according to Atlas specifications.

    Attributes:
        name (str): Name of the function.
        qualified_name (str): Fully qualified name of the function.
        schema_qualified_name (str): Qualified name of the schema containing the function.
        schema_name (str): Name of the schema containing the function.
        database_name (str): Name of the database containing the function.
        database_qualified_name (str): Qualified name of the database.
        connection_qualified_name (str): Qualified name of the connection.
        function_type (str): Type of function (Scalar or Tabular).
        function_return_type (str): Return type of the function.
        function_language (str): Language the function is written in.
        function_definition (str): Source code or definition of the function.
        function_arguments (List[str]): List of function argument definitions.
        function_is_secure (bool): Whether the function is secure.
        function_is_external (bool): Whether the function is external.
        function_is_d_m_f (bool): Whether the function is a data metric function.
        function_is_memoizable (bool): Whether the function results can be memoized.
    """

    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: None = None,
        database_name: None = None,
        database_qualified_name: None = None,
        connection_qualified_name: None = None,
    ) -> "Function": ...

    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: str,
        database_name: str,
        database_qualified_name: str,
        connection_qualified_name: str,
    ) -> "Function": ...

    @classmethod
    @init_guid
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: Optional[str] = None,
        database_name: Optional[str] = None,
        database_qualified_name: Optional[str] = None,
        connection_qualified_name: Optional[str] = None,
    ) -> "Function":
        """Create a new Function entity.

        Args:
            name (str): Name of the function.
            schema_qualified_name (str): Qualified name of the schema.
            schema_name (Optional[str], optional): Name of the schema. Defaults to None.
            database_name (Optional[str], optional): Name of the database. Defaults to None.
            database_qualified_name (Optional[str], optional): Qualified name of the database.
                Defaults to None.
            connection_qualified_name (Optional[str], optional): Qualified name of the connection.
                Defaults to None.

        Returns:
            Function: The created Function entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        validate_required_fields(
            ["name", "schema_qualified_name"], [name, schema_qualified_name]
        )
        attributes = Function.Attributes.create(
            name=name,
            schema_qualified_name=schema_qualified_name,
            schema_name=schema_name,
            database_name=database_name,
            database_qualified_name=database_qualified_name,
            connection_qualified_name=connection_qualified_name,
        )
        return cls(attributes=attributes)

    class Attributes(assets.Function.Attributes):
        """Attributes for Function entities.

        This class defines the attributes specific to Function entities.

        Attributes:
            function_arguments (Set[str] | None): Set of function arguments.
        """

        # overriding function_arguments same as in super class
        function_arguments: Optional[Set[str]] = None

        @classmethod
        @init_guid
        def create(
            cls,
            *,
            name: str,
            schema_qualified_name: str,
            schema_name: Optional[str] = None,
            database_name: Optional[str] = None,
            database_qualified_name: Optional[str] = None,
            connection_qualified_name: Optional[str] = None,
        ) -> "Function.Attributes":
            """Create a new Function.Attributes instance.

            Args:
                name (str): Name of the function.
                schema_qualified_name (str): Qualified name of the schema.
                schema_name (Optional[str], optional): Name of the schema. Defaults to None.
                database_name (Optional[str], optional): Name of the database. Defaults to None.
                database_qualified_name (Optional[str], optional): Qualified name of the database.
                    Defaults to None.
                connection_qualified_name (Optional[str], optional): Qualified name of the connection.
                    Defaults to None.

            Returns:
                Function.Attributes: The created attributes instance.
            """
            validate_required_fields(
                ["name", "schema_qualified_name"], [name, schema_qualified_name]
            )
            if connection_qualified_name:
                connector_name = AtlanConnectorType.get_connector_name(
                    connection_qualified_name
                )
            else:
                result = AtlanConnectorType.get_connector_name(
                    schema_qualified_name, "schema_qualified_name", 5
                )
                if isinstance(result, tuple) and len(result) == 2:
                    connection_qn, connector_name_result = result
                    # Ensure connector_name is a string
                    connector_name = (
                        str(connector_name_result)
                        if connector_name_result
                        else "unknown"
                    )
                else:
                    raise ValueError(
                        f"Invalid result from AtlanConnectorType.get_connector_name: {result}"
                    )

            fields = schema_qualified_name.split("/")
            qualified_name = f"{schema_qualified_name}/{name}"
            connection_qualified_name = connection_qualified_name or connection_qn
            database_name = database_name or fields[3]
            schema_name = schema_name or fields[4]
            database_qualified_name = (
                database_qualified_name
                or f"{connection_qualified_name}/{database_name}"
            )
            function_schema = Schema.ref_by_qualified_name(schema_qualified_name)

            return Function.Attributes(
                name=name,
                qualified_name=qualified_name,
                database_name=database_name,
                database_qualified_name=database_qualified_name,
                schema_name=schema_name,
                schema_qualified_name=schema_qualified_name,
                connector_name=connector_name,
                connection_qualified_name=connection_qualified_name,
                function_schema=function_schema,
            )

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a Function entity.

        Args:
            obj (Dict[str, Any]): Dictionary containing function metadata.

        Returns:
            assets.Function: The created Function entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        try:
            assert (
                "function_name" in obj and obj["function_name"] is not None
            ), "Function name cannot be None"
            assert (
                "argument_signature" in obj and obj["argument_signature"] is not None
            ), "Function argument signature cannot be None"
            assert (
                "function_definition" in obj and obj["function_definition"] is not None
            ), "Function definition cannot be None"
            assert (
                "is_external" in obj and obj["is_external"] is not None
            ), "Function is_external name cannot be None"
            assert (
                "is_memoizable" in obj and obj["is_memoizable"] is not None
            ), "Function is_memoizable cannot be None"
            assert (
                "function_language" in obj and obj["function_language"] is not None
            ), "Function language cannot be None"
            assert (
                "function_catalog" in obj and obj["function_catalog"] is not None
            ), "Function catalog cannot be None"
            assert (
                "function_schema" in obj and obj["function_schema"] is not None
            ), "Function schema cannot be None"

            function_attributes = {}
            function_custom_attributes = {}

            function_attributes["name"] = obj["function_name"]
            function_attributes["qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["function_catalog"],
                obj["function_schema"],
                obj["function_name"],
            )
            function_attributes["database_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"], obj["function_catalog"]
            )
            function_attributes["schema_qualified_name"] = build_atlas_qualified_name(
                obj["connection_qualified_name"],
                obj["function_catalog"],
                obj["function_schema"],
            )
            function_attributes["connection_qualified_name"] = obj[
                "connection_qualified_name"
            ]
            function_attributes["schema_name"] = obj["function_schema"]
            function_attributes["database_name"] = obj["function_catalog"]

            data_type = obj.get("data_type", "")
            if data_type and "TABLE" in data_type:
                function_attributes["function_type"] = "Tabular"
            else:
                function_attributes["function_type"] = "Scalar"
            function_attributes["function_return_type"] = obj.get("data_type", None)
            function_attributes["function_language"] = obj.get(
                "function_language", None
            )
            function_attributes["function_definition"] = obj.get(
                "function_definition", None
            )
            function_attributes["function_arguments"] = list(
                obj.get("argument_signature", "()")[1:-1].split(",")
            )
            function_attributes["function_is_secure"] = (
                obj.get("is_secure", None) == "YES"
            )
            function_attributes["function_is_external"] = (
                obj.get("is_external", None) == "YES"
            )
            function_attributes["function_is_d_m_f"] = (
                obj.get("is_data_metric", None) == "YES"
            )
            function_attributes["function_is_memoizable"] = (
                obj.get("is_memoizable", None) == "YES"
            )

            return {
                "attributes": function_attributes,
                "custom_attributes": function_custom_attributes,
                "entity_class": Function,
            }
        except AssertionError as e:
            raise ValueError(f"Error creating Function Entity: {str(e)}")


class TagAttachment(assets.TagAttachment):
    """Tag attachment entity transformer for Atlas.

    This class handles the transformation of tag attachment metadata into Atlas
    TagAttachment entities.
    """

    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: None = None,
        database_name: None = None,
        database_qualified_name: None = None,
        connection_qualified_name: None = None,
    ) -> "TagAttachment": ...

    @overload
    @classmethod
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: str,
        database_name: str,
        database_qualified_name: str,
        connection_qualified_name: str,
    ) -> "TagAttachment": ...

    @classmethod
    @init_guid
    def creator(
        cls,
        *,
        name: str,
        schema_qualified_name: str,
        schema_name: Optional[str] = None,
        database_name: Optional[str] = None,
        database_qualified_name: Optional[str] = None,
        connection_qualified_name: Optional[str] = None,
    ) -> "TagAttachment":
        """Create a new TagAttachment entity.

        Args:
            name (str): Name of the tag attachment.
            schema_qualified_name (str): Qualified name of the schema.
            schema_name (Optional[str], optional): Name of the schema. Defaults to None.
            database_name (Optional[str], optional): Name of the database. Defaults to None.
            database_qualified_name (Optional[str], optional): Qualified name of the database.
                Defaults to None.
            connection_qualified_name (Optional[str], optional): Qualified name of the connection.
                Defaults to None.

        Returns:
            TagAttachment: The created TagAttachment entity.
        """
        validate_required_fields(
            ["name", "schema_qualified_name"], [name, schema_qualified_name]
        )
        attributes = TagAttachment.Attributes.create(
            name=name,
            schema_qualified_name=schema_qualified_name,
            connection_qualified_name=connection_qualified_name,
        )
        return cls(attributes=attributes)

    class Attributes(assets.TagAttachment.Attributes):
        """Attributes for TagAttachment entities.

        This class defines the attributes specific to TagAttachment entities.
        """

        @classmethod
        @init_guid
        def create(
            cls,
            *,
            name: str,
            schema_qualified_name: str,
            connection_qualified_name: Optional[str] = None,
        ) -> "TagAttachment.Attributes":
            """Create a new TagAttachment.Attributes instance.

            Args:
                name (str): Name of the tag attachment.
                schema_qualified_name (str): Qualified name of the schema.
                connection_qualified_name (Optional[str], optional): Qualified name of the connection.
                    Defaults to None.

            Returns:
                TagAttachment.Attributes: The created attributes instance.
            """
            validate_required_fields(
                ["name", "schema_qualified_name"], [name, schema_qualified_name]
            )
            if connection_qualified_name:
                connector_name = AtlanConnectorType.get_connector_name(
                    connection_qualified_name
                )
            else:
                result = AtlanConnectorType.get_connector_name(
                    schema_qualified_name, "schema_qualified_name", 5
                )
                if isinstance(result, tuple) and len(result) == 2:
                    connection_qn, connector_name_result = result
                    # Ensure connector_name is a string
                    connector_name = (
                        str(connector_name_result)
                        if connector_name_result
                        else "unknown"
                    )
                else:
                    # Handle the case where result is not a tuple
                    raise ValueError(
                        f"Invalid result from AtlanConnectorType.get_connector_name: {result}"
                    )

            qualified_name = f"{schema_qualified_name}/{name}"
            connection_qualified_name = connection_qualified_name or connection_qn

            return TagAttachment.Attributes(
                name=name,
                qualified_name=qualified_name,
                connector_name=connector_name,
                connection_qualified_name=connection_qualified_name,
            )

    @classmethod
    def get_attributes(cls, obj: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a dictionary into a TagAttachment entity.

        Args:
            obj (Dict[str, Any]): Dictionary containing tag attachment metadata.

        Returns:
            assets.TagAttachment: The created TagAttachment entity.

        Raises:
            ValueError: If required fields are missing or invalid.
        """
        try:
            assert (
                "tag_name" in obj and obj["tag_name"] is not None
            ), "Tag name cannot be None"
            assert (
                "tag_database" in obj and obj["tag_database"] is not None
            ), "Tag database cannot be None"
            assert (
                "tag_schema" in obj and obj["tag_schema"] is not None
            ), "Tag schema cannot be None"
            assert (
                "object_database" in obj and obj["object_database"] is not None
            ), "Object database cannot be None"
            assert (
                "object_schema" in obj and obj["object_schema"] is not None
            ), "Object schema cannot be None"

            tag_attachment_attributes = {}
            tag_attachment_custom_attributes = {}

            tag_attachment_attributes["name"] = obj["tag_name"]
            tag_attachment_attributes["connection_qualified_name"] = obj[
                "connection_qualified_name"
            ]
            tag_attachment_attributes["database_qualified_name"] = (
                build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["tag_database"],
                )
            )
            tag_attachment_attributes["schema_qualified_name"] = (
                build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["tag_database"],
                    obj["tag_schema"],
                )
            )

            tag_attachment_attributes["tag_qualified_name"] = (
                build_atlas_qualified_name(
                    obj["connection_qualified_name"],
                    obj["tag_database"],
                    obj["tag_schema"],
                    obj["tag_name"],
                )
            )
            object_cat = obj.get("object_database", "")
            object_schema = obj.get("object_schema", "")

            tag_attachment_attributes["object_database_qualified_name"] = (
                build_atlas_qualified_name(obj["connection_qualified_name"], object_cat)
            )
            tag_attachment_attributes["object_schema_qualified_name"] = (
                build_atlas_qualified_name(
                    obj["connection_qualified_name"], object_cat, object_schema
                )
            )
            tag_attachment_attributes["object_database_name"] = object_cat
            tag_attachment_attributes["object_schema_name"] = object_schema
            tag_attachment_attributes["object_domain"] = obj.get("domain", None)
            tag_attachment_attributes["object_name"] = obj.get("object_name", None)
            tag_attachment_attributes["database_name"] = obj["tag_database"]
            tag_attachment_attributes["schema_name"] = obj["tag_schema"]
            tag_attachment_attributes["source_tag_id"] = obj.get("tag_id", None)
            tag_attachment_attributes["tag_attachment_string_value"] = obj.get(
                "tag_value", None
            )

            if object_domain := obj.get("domain", None):
                object_cat = obj.get("object_cat", "")
                object_schema = obj.get("object_schema", "")
                object_name = obj.get("object_name", "")
                column_name = obj.get("column_name", "")

                object_qualified_name = ""
                if object_domain == "DATABASE":
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"], object_cat, object_name
                    )
                elif object_domain == "SCHEMA":
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        object_cat,
                        object_schema,
                        object_name,
                    )
                elif object_domain in ["TABLE", "STREAM", "PIPE"]:
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        object_cat,
                        object_schema,
                        object_name,
                    )
                elif object_domain == "COLUMN":
                    object_qualified_name = build_atlas_qualified_name(
                        obj["connection_qualified_name"],
                        object_cat,
                        object_schema,
                        object_name,
                        column_name,
                    )

                tag_attachment_attributes["object_qualified_name"] = (
                    object_qualified_name
                )

            if classification_defs := obj.get("classification_defs", []):
                tag_name = obj.get("tag_name", "").upper()
                matching_defs = [
                    c
                    for c in classification_defs
                    if c.get("displayName", "").upper() == tag_name
                ]

                if matching_defs:
                    oldest_def = min(
                        matching_defs, key=lambda x: x.get("createTime", float("inf"))
                    )
                    tag_attachment_attributes["mapped_classification_name"] = (
                        json.dumps(oldest_def.get("name"))
                    )
                else:
                    tag_attachment_attributes["mapped_classification_name"] = (
                        json.dumps(obj.get("mappedClassificationName", ""))
                    )
            else:
                tag_attachment_attributes["mapped_classification_name"] = json.dumps(
                    obj.get("mappedClassificationName", "")
                )

            return {
                "attributes": tag_attachment_attributes,
                "custom_attributes": tag_attachment_custom_attributes,
                "entity_class": TagAttachment,
            }
        except Exception as e:
            raise ValueError(f"Error creating TagAttachment Entity: {str(e)}")
