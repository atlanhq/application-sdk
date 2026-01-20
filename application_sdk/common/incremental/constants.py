"""Constants for incremental extraction.

This module contains configuration constants for incremental metadata extraction.
"""

# Prefix for storing marker timestamp and current state of a connection in ObjectStore.
PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE = (
    "persistent-artifacts/apps/{application_name}/connection/{connection_id}"
)

# Maximum number of column extraction batch activities to execute in parallel
# Controls concurrency during incremental column extraction
MAX_CONCURRENT_COLUMN_BATCHES = 3

# Subpath template for per-run incremental diff (under connection prefix)
# Full path: {PERSISTENT_ARTIFACTS_S3_PREFIX_TEMPLATE}/{INCREMENTAL_DIFF_SUBPATH_TEMPLATE}
# Example: persistent-artifacts/apps/oracle/connection/123456/runs/abc-def-ghi/incremental-diff
INCREMENTAL_DIFF_SUBPATH_TEMPLATE = "runs/{run_id}/incremental-diff"

# Format to resolve the marker timestamp for following runs of a connection
# Example: 2025-12-08T10:00:00Z
MARKER_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Default incremental state for first run (when incremental_state field doesn't exist)
# Required by coalesce function in DuckDB
INCREMENTAL_DEFAULT_STATE = "NO CHANGE"

# DuckDB configuration constants
# Base folder for DuckDB temp files (each connection gets a unique UUID subfolder)
DUCKDB_COMMON_TEMP_FOLDER = "/tmp/incremental_duckdb"

# Default memory limit for DuckDB (fixed for K8s pods)
DUCKDB_DEFAULT_MEMORY_LIMIT = "2GB"
