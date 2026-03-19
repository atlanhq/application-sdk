"""Deprecated: use application_sdk.common.sql_utils instead."""

import warnings

warnings.warn(
    "application_sdk.activities.common.sql_utils is deprecated and will be removed in v3.1.0. "
    "Use application_sdk.common.sql_utils instead.",
    DeprecationWarning,
    stacklevel=2,
)
from application_sdk.common.sql_utils import (  # noqa: E402, F401
    execute_multidb_flow,
    execute_single_db,
    finalize_multidb_results,
    prepare_database_query,
    setup_database_connection,
)
