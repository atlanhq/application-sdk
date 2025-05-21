# Configuration

The Application SDK uses environment variables for configuration. These can be set directly in the environment or through a `.env` file. The configuration options are organized into several categories.

## Application Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_APPLICATION_NAME` | Name of the application, used for identification | `default` |
| `ATLAN_APP_HTTP_HOST` | Host address for the application's HTTP server | `localhost` |
| `ATLAN_APP_HTTP_PORT` | Port number for the application's HTTP server | `8000` |
| `ATLAN_TENANT_ID` | Tenant ID for multi-tenant applications | `default` |
| `ATLAN_APP_DASHBOARD_HOST` | Host address for the application's dashboard | `localhost` |
| `ATLAN_APP_DASHBOARD_PORT` | Port number for the application's dashboard | `8000` |
| `ATLAN_SQL_SERVER_MIN_VERSION` | Minimum required SQL Server version | `None` |
| `ATLAN_SQL_QUERIES_PATH` | Path to the SQL queries directory | `app/sql` |

## Workflow Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_WORKFLOW_HOST` | Host address for the Temporal server | `localhost` |
| `ATLAN_WORKFLOW_PORT` | Port number for the Temporal server | `7233` |
| `ATLAN_WORKFLOW_NAMESPACE` | Namespace for Temporal workflows | `default` |
| `ATLAN_WORKFLOW_UI_HOST` | Host address for the Temporal UI | `localhost` |
| `ATLAN_WORKFLOW_UI_PORT` | Port number for the Temporal UI | `8233` |
| `ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS` | Maximum timeout duration for workflows (in hours) | `1` |
| `ATLAN_MAX_CONCURRENT_ACTIVITIES` | Maximum number of activities that can run concurrently | `5` |
| `ATLAN_HEARTBEAT_TIMEOUT` | Timeout duration for activity heartbeats (in seconds) | `300` |
| `ATLAN_START_TO_CLOSE_TIMEOUT` | Maximum duration an activity can run before timing out (in seconds) | `7200` |

## SQL Client Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_SQL_USE_SERVER_SIDE_CURSOR` | Whether to use server-side cursors for SQL operations | `true` |

## DAPR Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `STATE_STORE_NAME` | Name of the state store component in DAPR | `statestore` |
| `SECRET_STORE_NAME` | Name of the secret store component in DAPR | `secretstore` |
| `OBJECT_STORE_NAME` | Name of the object store component in DAPR | `objectstore` |

## Observability Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_OBSERVABILITY_DIR` | Base directory for storing observability data | `/tmp/observability` |
| `ATLAN_ENABLE_HIVE_PARTITIONING` | Whether to enable Hive partitioning for observability data | `true` |
| `ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK` | Whether to enable Dapr sink for observability data | `true` |

## Logging Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `LOG_LEVEL` | Log level for the application (DEBUG, INFO, WARNING, ERROR, CRITICAL) | `INFO` |
| `LOG_BATCH_SIZE` | Number of log records to buffer before writing to parquet file | `100` |
| `LOG_FLUSH_INTERVAL_SECONDS` | Time interval (in seconds) to flush logs to parquet file | `10` |
| `LOG_RETENTION_DAYS` | Number of days to retain log records before automatic cleanup | `30` |
| `LOG_CLEANUP_ENABLED` | Whether to enable automatic cleanup of old logs | `false` |
| `LOG_FILE_NAME` | Name of the parquet file used for log storage | `log.parquet` |

## Metrics Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ENABLE_OTLP_METRICS` | Whether to enable OpenTelemetry metrics export | `false` |
| `METRICS_BATCH_SIZE` | Number of metric records to buffer before writing to parquet file | `100` |
| `METRICS_FLUSH_INTERVAL_SECONDS` | Time interval (in seconds) to flush metrics to parquet file | `10` |
| `METRICS_RETENTION_DAYS` | Number of days to retain metric records before automatic cleanup | `30` |
| `METRICS_CLEANUP_ENABLED` | Whether to enable automatic cleanup of old metrics | `false` |
| `METRICS_FILE_NAME` | Name of the parquet file used for metric storage | `metrics.parquet` |

## Traces Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ENABLE_OTLP_TRACES` | Whether to enable OpenTelemetry traces export | `false` |
| `TRACES_BATCH_SIZE` | Number of trace records to buffer before writing to parquet file | `100` |
| `TRACES_FLUSH_INTERVAL_SECONDS` | Time interval (in seconds) to flush traces to parquet file | `5` |
| `TRACES_RETENTION_DAYS` | Number of days to retain trace records before automatic cleanup | `30` |
| `TRACES_CLEANUP_ENABLED` | Whether to enable automatic cleanup of old traces | `true` |
| `TRACES_FILE_NAME` | Name of the parquet file used for trace storage | `traces.parquet` |

## OpenTelemetry Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `OTEL_SERVICE_NAME` | Service name for OpenTelemetry | `atlan-application-sdk` |
| `OTEL_SERVICE_VERSION` | Service version for OpenTelemetry | `0.1.0` |
| `OTEL_RESOURCE_ATTRIBUTES` | Additional resource attributes for OpenTelemetry | `""` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint for the OpenTelemetry collector | `http://localhost:4317` |
| `ENABLE_OTLP_LOGS` | Whether to enable OpenTelemetry log export | `false` |
| `ENABLE_OTLP_METRICS` | Whether to enable OpenTelemetry metrics export | `false` |
| `ENABLE_OTLP_TRACES` | Whether to enable OpenTelemetry traces export | `false` |
| `OTEL_WF_NODE_NAME` | Node name for workflow telemetry | `""` |
| `OTEL_EXPORTER_TIMEOUT_SECONDS` | Timeout for OpenTelemetry exporters in seconds | `30` |
| `OTEL_BATCH_DELAY_MS` | Delay between batch exports in milliseconds | `5000` |
| `OTEL_BATCH_SIZE` | Maximum size of export batches | `512` |
| `OTEL_QUEUE_SIZE` | Maximum size of the export queue | `2048` |

## Note

Most configuration options have sensible defaults, but can be overridden by setting the corresponding environment variables. You can set these variables either in your environment or by creating a `.env` file in your project root.
