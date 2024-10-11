from pydantic import BaseModel, Field


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


## Database


class DatabaseEntity(BaseObjectEntity):
    namespace: Namespace
    package: Package


## Schema


class SchemaEntity(BaseObjectEntity):
    namespace: Namespace
    package: Package


## Table


class TableEntity(BaseObjectEntity):
    isPartition: bool
    isSearchable: bool = True
    namespace: Namespace
    package: Package


## View


class ViewEntity(BaseObjectEntity):
    isSearchable: bool = True
    namespace: Namespace
    package: Package


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
