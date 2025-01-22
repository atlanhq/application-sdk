from enum import Enum


class SQLConstants(Enum):
    """
    Constants for SQL handler
    """

    DATABASE_ALIAS_KEY = "catalog_name"
    SCHEMA_ALIAS_KEY = "schema_name"
    DATABASE_RESULT_KEY = "TABLE_CATALOG"
    SCHEMA_RESULT_KEY = "TABLE_SCHEMA"
