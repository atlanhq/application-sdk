import io
import os
from pathlib import Path
from typing import Any, Dict

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
