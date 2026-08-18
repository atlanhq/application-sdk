"""Workflow-config persistence for the server surface."""

from server_sdk.config.store import (
    CONFIG_KEY_PATTERN,
    ConfigStore,
    LocalFileConfigStore,
    application_name,
    config_objectstore_key,
)

__all__ = [
    "CONFIG_KEY_PATTERN",
    "ConfigStore",
    "LocalFileConfigStore",
    "S3ConfigStore",
    "application_name",
    "config_objectstore_key",
    "default_config_store",
]


def __getattr__(name: str):  # lazy: keep boto3-adjacent module unimported by default
    if name in ("S3ConfigStore", "default_config_store"):
        from server_sdk.config import s3

        return getattr(s3, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
