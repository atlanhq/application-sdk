import json
from typing import Any, Dict

from pydantic import BaseModel, ConfigDict, Field


class BaseSchemaModel(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        json_encoders={
            # Add any custom type encoders here if needed
        },
    )

    def model_dump(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        kwargs.setdefault("by_alias", True)
        return super().model_dump(*args, **kwargs)

    def json(self, *args: Any, **kwargs: Any) -> str:
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(*args, **kwargs)


class Namespace(BaseSchemaModel):
    id: str
    name: str
    version: int = 1


class Package(BaseSchemaModel):
    id: str
    name: str
    version: int = 1


class BaseObjectEntity(BaseSchemaModel):
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


class ColumnConstraint(BaseSchemaModel):
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


class PydanticJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, BaseModel):
            return o.model_dump()
        return super().default(o)
