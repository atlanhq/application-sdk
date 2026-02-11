"""Factory function to load a DataSourceFixture from a YAML config file."""

from pathlib import Path
from typing import Union

import yaml

from application_sdk.test_utils.e2e.fixtures.base import DataSourceFixture
from application_sdk.test_utils.e2e.fixtures.containerized import ContainerizedFixture
from application_sdk.test_utils.e2e.fixtures.schema import ContainerizedDatasourceConfig


def load_fixture_from_yaml(yaml_path: Union[str, Path]) -> DataSourceFixture:
    """Parse a datasource YAML file and return the appropriate fixture instance.

    Args:
        yaml_path: Path to the datasource.yaml file.

    Returns:
        A DataSourceFixture ready to call setup() on.

    Raises:
        FileNotFoundError: If the YAML file does not exist.
        pydantic.ValidationError: If the YAML content is invalid.
    """
    path = Path(yaml_path)
    if not path.exists():
        raise FileNotFoundError(f"Datasource config not found: {path}")

    raw = yaml.safe_load(path.read_text())
    config = ContainerizedDatasourceConfig.model_validate(raw)

    return ContainerizedFixture(config, yaml_dir=path.parent)
