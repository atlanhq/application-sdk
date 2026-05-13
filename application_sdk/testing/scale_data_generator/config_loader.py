from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from application_sdk.errors import InvalidInputError


class OutputFormat(Enum):
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


class ConfigLoader:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config: dict[str, Any] | None = None
        self._load_config()

    def _load_config(self) -> None:
        """Load and validate the YAML configuration file."""
        try:
            with open(self.config_path, "r") as file:
                self.config = yaml.safe_load(file)
            self._validate_config()
        except FileNotFoundError:
            raise FileNotFoundError(f"Config file not found at: {self.config_path}")
        except yaml.YAMLError as e:
            raise InvalidInputError(
                message=f"Invalid YAML format: {e!s}",
                field="config_path",
                value_summary=str(self.config_path),
            ) from e

    def _validate_config(self) -> None:
        """Validate the configuration structure."""
        required_sections = ["database", "hierarchy", "schema"]
        for section in required_sections:
            if section not in self.config:
                raise InvalidInputError(
                    message=f"Missing required section: {section}",
                    field=section,
                )

        # Validate schema references
        schema_tables = {schema["name"] for schema in self.config["schema"]}
        hierarchy_tables = self._get_hierarchy_tables(self.config["hierarchy"][0])

        if schema_tables != hierarchy_tables:
            raise InvalidInputError(
                message="Mismatch between schema and hierarchy table definitions",
                field="schema_or_hierarchy",
            )

    def _get_hierarchy_tables(
        self, hierarchy: dict[str, Any], tables: set[str] | None = None
    ) -> set[str]:
        """Recursively get all table names from hierarchy."""
        if tables is None:
            tables = set()

        tables.add(hierarchy["name"])
        if "children" in hierarchy:
            for child in hierarchy["children"]:
                self._get_hierarchy_tables(child, tables)

        return tables

    def get_table_schema(self, table_name: str) -> dict[str, Any]:
        """Get schema definition for a specific table."""
        for schema in self.config["schema"]:
            if schema["name"] == table_name:
                return schema
        raise InvalidInputError(
            message=f"Schema not found for table: {table_name}",
            field="table_name",
            value_summary=table_name,
        )

    def get_hierarchy(self) -> dict[str, Any]:
        """Get the hierarchy configuration."""
        return self.config["hierarchy"][0]

    def get_database(self) -> dict[str, Any]:
        """Get the database configuration."""
        return self.config["database"][0]
