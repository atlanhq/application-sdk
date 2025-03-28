from pathlib import Path
from typing import Any, Dict, Optional

import yaml

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
        if not self.config:
            raise ValueError("Config is empty")
            
        if "hierarchy" not in self.config:
            raise ValueError("Missing required section: hierarchy")

        # Validate hierarchy structure
        hierarchy = self.config["hierarchy"]
        if not isinstance(hierarchy, list) or len(hierarchy) != 1:
            raise ValueError("Hierarchy must be a list with exactly one root element")

    def get_hierarchy(self) -> Dict[str, Any]:
        """Get the hierarchy configuration."""
        if not self.config or "hierarchy" not in self.config:
            raise ValueError("No hierarchy configuration found")
        return self.config["hierarchy"][0]

    def get_table_records(self, table_name: str) -> int:
        """Get the number of records for a specific table in the hierarchy."""
        def find_table_records(hierarchy: Dict[str, Any], target_name: str) -> Optional[int]:
            if hierarchy["name"] == target_name:
                return hierarchy.get("records", 0)
            if "children" in hierarchy:
                for child in hierarchy["children"]:
                    result = find_table_records(child, target_name)
                    if result is not None:
                        return result
            return None

        hierarchy = self.get_hierarchy()
        records = find_table_records(hierarchy, table_name)
        if records is None:
            raise ValueError(f"Table not found in hierarchy: {table_name}")
        return records

    def get_table_children(self, table_name: str) -> list[Dict[str, Any]]:
        """Get the children of a specific table in the hierarchy."""
        def find_table_children(hierarchy: Dict[str, Any], target_name: str) -> Optional[list[Dict[str, Any]]]:
            if hierarchy["name"] == target_name:
                return hierarchy.get("children", [])
            if "children" in hierarchy:
                for child in hierarchy["children"]:
                    result = find_table_children(child, target_name)
                    if result is not None:
                        return result
            return None

        hierarchy = self.get_hierarchy()
        children = find_table_children(hierarchy, table_name)
        if children is None:
            raise ValueError(f"Table not found in hierarchy: {table_name}")
        return children
