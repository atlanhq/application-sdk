from enum import Enum


class SQLConstants(Enum):
    """
    Constants for SQL handler
    """

    DATABASE_ALIAS_KEY = "catalog_name"
    SCHEMA_ALIAS_KEY = "schema_name"
    DATABASE_RESULT_KEY = "database_name"
    SCHEMA_RESULT_KEY = "schema_name"
