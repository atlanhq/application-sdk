"""Workflow-config persistence for the server surface."""

from application_sdk_api.config.env import APPENV_PREFIX, AppEnv, prefix_for
from application_sdk_api.config.store import (
    CONFIG_KEY_PATTERN,
    ConfigStore,
    LocalFileConfigStore,
    application_name,
    config_objectstore_key,
)

__all__ = [
    "APPENV_PREFIX",
    "AppEnv",
    "CONFIG_KEY_PATTERN",
    "ConfigStore",
    "LocalFileConfigStore",
    "S3ConfigStore",
    "application_name",
    "config_objectstore_key",
    "default_config_store",
    "prefix_for",
]


def __getattr__(name: str):  # lazy: keep boto3-adjacent module unimported by default
    if name in ("S3ConfigStore", "default_config_store"):
        from application_sdk_api.config import s3

        return getattr(s3, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
