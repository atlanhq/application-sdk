"""Typed error leaves for the Atlas SQL transformer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from application_sdk.errors.leaves import DataIntegrityError


@dataclass(kw_only=True)
class TransformProcedureError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_PROCEDURE"
    message: str = "Failed to transform procedure entity"
    expectation: str | None = "valid_procedure_attributes"
    location: str | None = "atlas/sql:Procedure"


@dataclass(kw_only=True)
class TransformDatabaseError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_DATABASE"
    message: str = "Failed to transform database entity"
    expectation: str | None = "valid_database_attributes"
    location: str | None = "atlas/sql:Database"


@dataclass(kw_only=True)
class TransformSchemaError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_SCHEMA"
    message: str = "Failed to transform schema entity"
    expectation: str | None = "valid_schema_attributes"
    location: str | None = "atlas/sql:Schema"


@dataclass(kw_only=True)
class TransformTableError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_TABLE"
    message: str = "Failed to transform table entity"
    expectation: str | None = "valid_table_attributes"
    location: str | None = "atlas/sql:Table"


@dataclass(kw_only=True)
class TransformColumnError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_COLUMN"
    message: str = "Failed to transform column entity"
    expectation: str | None = "valid_column_attributes"
    location: str | None = "atlas/sql:Column"


@dataclass(kw_only=True)
class TransformFunctionError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_FUNCTION"
    message: str = "Failed to transform function entity"
    expectation: str | None = "valid_function_attributes"
    location: str | None = "atlas/sql:Function"


@dataclass(kw_only=True)
class TransformTagAttachmentError(DataIntegrityError):
    code: ClassVar[str] = "DATA_INTEGRITY_TRANSFORM_TAG_ATTACHMENT"
    message: str = "Failed to transform tag attachment entity"
    expectation: str | None = "valid_tag_attachment_attributes"
    location: str | None = "atlas/sql:TagAttachment"
