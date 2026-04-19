from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class OutputFormat(Enum):
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


class ConfigLoader:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config: Optional[Dict[str, Any]] = None
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
            raise ValueError(f"Invalid YAML format: {str(e)}")

    def _validate_config(self) -> None:
        """Validate the configuration structure."""
        required_sections = ["database", "hierarchy", "schema"]
        for section in required_sections:
            if section not in self.config:
                raise ValueError(f"Missing required section: {section}")

        # Validate schema references
        schema_tables = {schema["name"] for schema in self.config["schema"]}
        hierarchy_tables = self._get_hierarchy_tables(self.config["hierarchy"][0])

        if schema_tables != hierarchy_tables:
            raise ValueError("Mismatch between schema and hierarchy table definitions")

    def _get_hierarchy_tables(
        self, hierarchy: Dict[str, Any], tables: Optional[set[str]] = None
    ) -> set[str]:
        """Recursively get all table names from hierarchy."""
        if tables is None:
            tables = set()

        tables.add(hierarchy["name"])
        if "children" in hierarchy:
            for child in hierarchy["children"]:
                self._get_hierarchy_tables(child, tables)

        return tables

    def get_table_schema(self, table_name: str) -> Dict[str, Any]:
        """Get schema definition for a specific table."""
        for schema in self.config["schema"]:
            if schema["name"] == table_name:
                return schema
        raise ValueError(f"Schema not found for table: {table_name}")

    def get_hierarchy(self) -> Dict[str, Any]:
        """Get the hierarchy configuration."""
        return self.config["hierarchy"][0]

    def get_database(self) -> Dict[str, Any]:
        """Get the database configuration."""
        return self.config["database"][0]
