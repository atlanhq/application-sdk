"""SSL utilities for HTTP clients, re-exported from :mod:`application_sdk.common.ssl_utils`.

They moved so the API host can use them without loading this package's
``__init__`` (the SQL client). Every name stays importable from here; patch
``application_sdk.common.ssl_utils`` to change what they read.
"""

from __future__ import annotations

import functools  # noqa: F401 — importable from here before the move
import os  # noqa: F401 — importable from here before the move
import ssl  # noqa: F401 — importable from here before the move

from application_sdk._logging import get_logger
from application_sdk.common.ssl_utils import (
    CERT_FILE_EXTENSIONS,
    SSL_CERT_DIR,
    _get_default_ca_bundle_path,
    _read_default_ca_certs,
    create_ssl_context_with_custom_certs,
    get_certificate_files,
    get_custom_ca_cert_bytes,
    get_ssl_cert_dir,
    get_ssl_context,
    logger,
)

__all__ = [
    "CERT_FILE_EXTENSIONS",
    "SSL_CERT_DIR",
    "create_ssl_context_with_custom_certs",
    "get_certificate_files",
    "get_custom_ca_cert_bytes",
    "get_logger",
    "get_ssl_cert_dir",
    "get_ssl_context",
    "logger",
    "_get_default_ca_bundle_path",
    "_read_default_ca_certs",
]
