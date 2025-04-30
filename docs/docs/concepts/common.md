# Common Utilities

This section describes various utility functions and classes found within the `application_sdk.common` package. These utilities provide foundational functionalities used across different parts of the SDK, such as logging, configuration management, interacting with AWS, and general helper functions.

## Logging (`logger_adaptors.py`)

The SDK uses the `loguru` library for enhanced logging capabilities, combined with standard Python logging and OpenTelemetry (OTLP) integration for structured, observable logs.

### Key Concepts

*   **`InterceptHandler`**: A standard `logging.Handler` that intercepts logs from standard Python logging (including libraries like `boto3`) and redirects them through `loguru`, ensuring consistent formatting and handling.
*   **`AtlanLoggerAdapter`**: The main interface for logging within the SDK. It wraps `loguru`, configures standard output format (including colors), handles OTLP exporter setup, and automatically enriches log messages with context.
    *   **Context Enrichment**: Automatically includes details from the current Temporal Workflow or Activity context (like `workflow_id`, `run_id`, `activity_id`, `attempt`, etc.) and FastAPI request context (`request_id`) if available.
    *   **OTLP Integration**: If `ENABLE_OTLP_LOGS` is true, logs are exported via the OpenTelemetry Protocol (OTLP) using `OTLPLogExporter`. Resource attributes (`service.name`, `service.version`, `k8s.workflow.node.name`, etc.) are automatically added based on environment variables (`OTEL_RESOURCE_ATTRIBUTES`, `OTEL_WF_NODE_NAME`, `SERVICE_NAME`, `SERVICE_VERSION`).
    *   **Custom Level**: Includes a custom `"ACTIVITY"` log level.
*   **Severity Mapping**: Maps standard log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL) and the custom ACTIVITY level to OpenTelemetry `SeverityNumber`.
*   **Configuration**: Log level (`LOG_LEVEL`), OTLP endpoint (`OTEL_EXPORTER_OTLP_ENDPOINT`), batching (`OTEL_BATCH_DELAY_MS`, `OTEL_BATCH_SIZE`), etc., are configured via environment variables defined in `application_sdk.constants`.

### Usage

The primary way to get a logger instance is via the `get_logger` function:

```python
from application_sdk.common.logger_adaptors import get_logger

# Get a logger instance, usually named after the module
logger = get_logger(__name__)

def my_function(data):
    logger.info(f"Processing data: {data}")
    try:
        # ... do something ...
        logger.activity("Data processing step completed successfully.") # Use custom activity level
    except Exception as e:
        logger.error(f"Failed during processing: {e}", exc_info=True) # Include stack trace

# In a Temporal Activity:
from temporalio import activity
logger = get_logger(__name__)
activity.logger = logger # Temporal integration

@activity.defn
async def my_activity():
    logger.info("Starting my activity...")
    # Logger automatically includes workflow/activity context
```

## AWS Utilities (`aws_utils.py`)

Provides helper functions specifically for interacting with AWS services, particularly RDS authentication.

### Key Functions

*   **`get_region_name_from_hostname(hostname)`**: Extracts the AWS region (e.g., `us-east-1`) from an RDS endpoint hostname.
*   **`generate_aws_rds_token_with_iam_role(...)`**: Assumes an IAM role using STS (`AssumeRole`) and then uses the temporary credentials to generate an RDS authentication token (`rds:GenerateDBAuthToken`). Requires `role_arn`, `host`, `user`. Optionally takes `external_id`, `session_name`, `port`, `region`.
*   **`generate_aws_rds_token_with_iam_user(...)`**: Generates an RDS authentication token directly using IAM user credentials (`aws_access_key_id`, `aws_secret_access_key`). Also requires `host`, `user`. Optionally takes `port`, `region`.

### Usage

These functions are typically used within a custom `Client`'s `load` method or a `Handler`'s credential handling logic when connecting to RDS databases using IAM authentication.

```python
# Example within a hypothetical Client's load method
from application_sdk.common.aws_utils import generate_aws_rds_token_with_iam_role

async def load(self, credentials: dict):
    auth_type = credentials.get("authType")
    if auth_type == "iam_role":
        password = generate_aws_rds_token_with_iam_role(
            role_arn=credentials.get("roleArn"),
            host=credentials.get("host"),
            user=credentials.get("username"),
            external_id=credentials.get("externalId"),
            region=credentials.get("region") # or determine automatically
        )
        # Use the generated token as the password for the connection
    # ... other authentication types ...
```

## General Utilities (`utils.py`)

Contains miscellaneous helper functions used throughout the SDK.

### Key Functions

*   **`prepare_query(query, workflow_args, temp_table_regex_sql)`**: Modifies a base SQL query string by formatting it with include/exclude filters, temporary table exclusion logic, and flags for excluding empty tables or views. Filters are sourced from `workflow_args["metadata"]`.
*   **`prepare_filters(include_filter_str, exclude_filter_str)`**: Parses JSON string filters (include/exclude) and converts them into normalized regex patterns suitable for SQL `WHERE` clauses (e.g., `db1.schema1|db1.schema2`).
*   **`normalize_filters(filter_dict, is_include)`**: Takes a dictionary defining filters (e.g., `{"db1": ["schema1", "schema2"], "db2": "*"}`) and converts it into a list of normalized regex strings.
*   **`get_workflow_config(config_id)`**: Retrieves workflow configuration data stored via `StateStoreInput`.
*   **`update_workflow_config(config_id, config)`**: Updates specific keys in a stored workflow configuration via `StateStoreOutput`.
*   **`read_sql_files(queries_prefix)`**: Recursively reads all `.sql` files from a specified directory (`queries_prefix`). Returns a dictionary mapping uppercase filenames (without `.sql`) to their string content. Useful for loading SQL queries used in activities.
*   **`get_actual_cpu_count()`**: Attempts to determine the number of CPUs available to the current process, considering potential container limits (via `os.sched_getaffinity`), falling back to `os.cpu_count()`.
*   **`get_safe_num_threads()`**: Calculates a reasonable number of threads for parallel processing, typically `get_actual_cpu_count() + 4`.
*   **`parse_credentials_extra(credentials)`**: Safely parses the `extra` field within a `credentials` dictionary (assuming it's a JSON string) and merges its contents back into the main dictionary.
*   **`run_sync(func)`**: A decorator (intended for internal use, e.g., in `AsyncBaseSQLClient`) to run a synchronous function (`func`) in a `ThreadPoolExecutor` to avoid blocking an asyncio event loop.

### Usage Examples

```python
# Reading SQL files for activities
from application_sdk.common.utils import read_sql_files

SQL_QUERIES = read_sql_files("/path/to/my/queries")
fetch_tables_query = SQL_QUERIES.get("FETCH_TABLES")

# Getting workflow config
from application_sdk.common.utils import get_workflow_config

config = get_workflow_config("my-config-id-123")
api_key = config.get("credentials", {}).get("apiKey")

# Preparing filters for a query
from application_sdk.common.utils import prepare_filters

include_pattern, exclude_pattern = prepare_filters(
    '{"prod_db": ["analytics", "reporting$"]}', # Include specific schemas in prod_db
    '{"dev_db": "*"}' # Exclude all of dev_db
)
# use patterns in SQL: WHERE table_schema SIMILAR TO '{include_pattern}' AND table_schema NOT SIMILAR TO '{exclude_pattern}'
```

## Summary

The `common` utilities provide essential services for logging, AWS integration, configuration management, and various helper tasks, forming a core part of the SDK's functionality and promoting consistent practices across different modules.