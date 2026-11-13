"""The worker's path to the obstore store constructors.

They live in :mod:`application_sdk._obstore_config`, which also ships in
``atlan-application-sdk-api`` so the consolidated API host builds its store with
the same code. This module re-exports them for the worker's existing imports
(``binding.py``, ``cloud.py``): one source of each constructor, no copy.
"""

from __future__ import annotations

from application_sdk._obstore_config import (
    log_obstore_config,
    logger,
    make_azure_store,
    make_gcs_store,
    make_s3_store,
    obstore_client_options,
    obstore_retry_config,
)

# Importable from here before the move; kept so the name stays where it was.
from application_sdk.observability.logger_adaptor import get_logger

__all__ = [
    "get_logger",
    "log_obstore_config",
    "logger",
    "make_azure_store",
    "make_gcs_store",
    "make_s3_store",
    "obstore_client_options",
    "obstore_retry_config",
]
