from typing import Any, Dict

from pydantic import BaseModel, Field

from application_sdk.workflows.transformers.utils import build_phoenix_uri


class Namespace(BaseModel):
    id: str
    name: str
    version: int = 1


class Package(BaseModel):
    id: str
    name: str
    version: int = 1


class BaseObjectEntity(BaseModel):
    typeName: str
    name: str
    URI: str
    sourceVersion: int = 1
    internalVersion: int = 1

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> "BaseObjectEntity":
        base_object = cls.model_validate(obj)
        return base_object


## Database


class DatabaseEntity(BaseObjectEntity):
    namespace: Namespace
    package: Package

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> "DatabaseEntity":
        database = DatabaseEntity.parse_obj(obj)
        database.name = obj["database_name"]
        database.typeName = obj["typeName"].capitalize()
        database.URI = build_phoenix_uri(
            obj["connector_name"], obj["connector_type"], obj["database_name"]
        )

        return database


## Schema


class SchemaEntity(BaseObjectEntity):
    namespace: Namespace
    package: Package

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> "SchemaEntity":
        schema = SchemaEntity.parse_obj(obj)
        schema.name = obj["schema_name"]
        schema.typeName = obj["typeName"].capitalize()
        schema.URI = build_phoenix_uri(
            obj["connector_name"],
            obj["connector_type"],
            obj["catalog_name"],
            obj["schema_name"],
        )

        return schema


## Table


class TableEntity(BaseObjectEntity):
    isPartition: bool
    isSearchable: bool = True
    namespace: Namespace
    package: Package

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> "TableEntity":
        table = TableEntity.parse_obj(obj)
        table.name = obj["table_name"]
        table.typeName = obj["typeName"].capitalize()
        table.URI = build_phoenix_uri(
            obj["connector_name"],
            obj["connector_type"],
            obj["table_catalog"],
            obj["table_schema"],
            obj["table_name"],
        )
        table.isPartition = obj.get("is_partition") or False

        return table


## Column


class ColumnConstraint(BaseModel):
    notNull: bool = False
    autoIncrement: bool = False
    primaryKey: bool = False
    uniqueKey: bool = False


class ColumnEntity(BaseObjectEntity):
    order: int
    dataType: str
    constraints: ColumnConstraint = Field(default_factory=ColumnConstraint)
    isSearchable: bool = True
    namespace: Namespace
    package: Package

    @classmethod
    def parse_obj(cls, obj: Dict[str, Any]) -> "ColumnEntity":
        column = ColumnEntity.parse_obj(obj)
        column.name = obj["column_name"]
        column.typeName = obj["typeName"].capitalize()
        column.URI = build_phoenix_uri(
            obj["connector_name"],
            obj["connector_type"],
            obj["table_catalog"],
            obj["table_schema"],
            obj["table_name"],
            obj["column_name"],
        )
        column.order = obj["ordinal_position"]
        column.dataType = obj["data_type"]
        column.constraints = ColumnConstraint(
            notNull=not obj.get("is_nullable") == "NO",
            autoIncrement=obj.get("is_autoincrement") == "YES",
        )

        return column


# utils
