from enum import Enum
from pathlib import Path
from typing import Any, Dict, List

import yaml


class OutputFormat(Enum):
    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


class ConfigLoader:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config: Dict[str, Any] = {}
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
        required_sections = [
            "hierarchy",
            "asset_template",
            "asset_definitions",
            "generator_config",
        ]
        for section in required_sections:
            if section not in self.config:
                raise ValueError(f"Missing required section: {section}")

        # Validate hierarchy structure
        hierarchy = self.config["hierarchy"]
        if not isinstance(hierarchy, list) or not hierarchy:
            raise ValueError("Hierarchy must be a non-empty list")

        # Validate asset templates
        templates = self.config["asset_template"]
        if not isinstance(templates, dict):
            raise ValueError("Asset templates must be a dictionary")

        # Validate asset definitions
        definitions = self.config["asset_definitions"]
        if not isinstance(definitions, dict):
            raise ValueError("Asset definitions must be a dictionary")

    def get_hierarchy(self) -> Dict[str, Any]:
        """Get the hierarchy configuration."""
        return self.config["hierarchy"][0]

    def get_asset_template(self, asset_type: str) -> Dict[str, Any]:
        """Get template configuration for a specific asset type."""
        templates = self.config["asset_template"]
        if asset_type not in templates:
            raise ValueError(f"Template not found for asset type: {asset_type}")
        return templates[asset_type]

    def get_asset_definition(self, asset_type: str) -> Dict[str, Any]:
        """Get asset definition for a specific type."""
        definitions = self.config["asset_definitions"]
        if asset_type not in definitions:
            raise ValueError(f"Definition not found for asset type: {asset_type}")
        return definitions[asset_type]

    def get_generator_config(self) -> Dict[str, Any]:
        """Get the generator configuration."""
        return self.config["generator_config"]

    def get_relationships(self) -> List[Dict[str, Any]]:
        """Get relationship definitions."""
        return self.config.get("relationships", [])
