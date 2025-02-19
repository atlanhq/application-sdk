import os
from typing import Any, Dict

import yaml


def load_config_from_yaml(yaml_file_path: str) -> Dict[str, Any]:
    """
    Method to load the configuration from the yaml file
    """
    yaml_file_path = os.path.join(os.getcwd(), yaml_file_path)
    with open(yaml_file_path) as yaml_file:
        return yaml.safe_load(yaml_file)
