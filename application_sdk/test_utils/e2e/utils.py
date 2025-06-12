import io
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import yaml


class ConfigurationError(Exception):
    """A configuration error has happened"""


def load_config_from_yaml(yaml_file_path: str) -> Dict[str, Any]:
    """
    Method to load the configuration from the yaml file
    """
    yaml_config_file = Path(yaml_file_path)
    if not yaml_config_file.is_file():
        raise ConfigurationError(f"Cannot open config file {yaml_config_file}")

    if yaml_config_file.suffix not in (".yaml", ".yml"):
        raise ConfigurationError(f"Config file is not a YAML file {yaml_config_file}")

    with yaml_config_file.open() as raw_yaml_config_file:
        raw_config = raw_yaml_config_file.read()
    expanded_config_file = os.path.expandvars(raw_config)
    config_fp = io.StringIO(expanded_config_file)
    try:
        return yaml.safe_load(config_fp)
    except yaml.error.YAMLError as e:
        raise ConfigurationError(
            f"YAML config file is not valid {yaml_config_file}: {e}"
        ) from e


def write_test_entities(
    entities: List[Dict[str, Any]],
    base_path: str,
    entity_type: str,
    batch_number: int = 1,
) -> None:
    """Write test entities to JSON files following the publish app's directory structure.

    This utility function writes entities to files matching the publish app's expected
    directory structure: base_path/entity_type/batch_number.json

    Args:
        entities: List of entity dictionaries to write
        base_path: Base directory (e.g. /tmp/data/first_run/default/postgres/1234/transformed)
        entity_type: Entity type in lowercase (e.g. table, column)
        batch_number: Batch number for the file name (default: 1)
    """
    # Create the full directory path
    dir_path = os.path.join(base_path, entity_type.lower())
    os.makedirs(dir_path, exist_ok=True)

    # Write entities to the file
    file_path = os.path.join(dir_path, f"{batch_number}.json")
    with open(file_path, "w") as f:
        for entity in entities:
            # Write each entity as a separate JSON line (JSONL format)
            f.write(json.dumps(entity) + "\n")


def create_test_entity(
    entity_type: str,
    qualified_name: str,
    name: str,
    columns: List[str] = None,
    connection_qualified_name: str = None,
    **extra_attrs,
) -> Dict[str, Any]:
    """Create a test entity with the correct structure.

    Args:
        entity_type: Type of entity (e.g. Table, Column)
        qualified_name: Full qualified name
        name: Entity name
        columns: Optional list of column names (for tables)
        connection_qualified_name: Optional connection qualified name
        **extra_attrs: Additional attributes to include

    Returns:
        Dict containing the entity data
    """
    # If connection_qualified_name not provided, extract from qualified_name
    if not connection_qualified_name:
        # Assuming qualified_name format: default/postgres/table_1
        connection_qualified_name = "/".join(qualified_name.split("/")[:-1])

    entity = {
        "customAttributes": {},
        "typeName": entity_type,
        "status": "ACTIVE",
        "attributes": {
            "name": name,
            "qualifiedName": qualified_name,
            "connectionQualifiedName": connection_qualified_name,
            "connectionName": "test_connection",
            "tenantId": connection_qualified_name.split("/")[0],
            "connectorName": connection_qualified_name.split("/")[1],
        },
    }

    # Add columns if provided
    if columns is not None:
        entity["attributes"]["columns"] = columns

    # Add any extra attributes
    entity["attributes"].update(extra_attrs)

    return entity
