# Configuration

The Application SDK uses environment variables for configuration. These can be set directly in the environment or through a `.env` file. The configuration options are organized into several categories.

## Application Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `ATLAN_APPLICATION_NAME` | Name of the application, used for identification and path generation | `default` | Used in object store paths, logging, and workflow identification |
| `ATLAN_DEPLOYMENT_NAME` | Name of the deployment, distinguishes between different deployments of the same application | `local` | Used to isolate resources between environments (dev, staging, prod) |
| `ATLAN_APP_HTTP_HOST` | Host address for the application's HTTP server | `localhost` | Bind address for FastAPI/HTTP server |
| `ATLAN_APP_HTTP_PORT` | Port number for the application's HTTP server | `8000` | Port for FastAPI/HTTP server |
| `ATLAN_TENANT_ID` | Tenant ID for multi-tenant applications | `default` | Used for tenant isolation in multi-tenant deployments |
| `ATLAN_APP_DASHBOARD_HOST` | Host address for the application's dashboard | `localhost` | Dashboard UI host for monitoring and management |
| `ATLAN_APP_DASHBOARD_PORT` | Port number for the application's dashboard | `8000` | Dashboard UI port for monitoring and management |
| `ATLAN_SQL_SERVER_MIN_VERSION` | Minimum required SQL Server version for compatibility checks | `None` | Validates SQL Server version during connection |
| `ATLAN_SQL_QUERIES_PATH` | Path to the SQL queries directory | `app/sql` | Location of SQL query files for metadata extraction |
| `ATLAN_TEMPORARY_PATH` | Path for storing temporary files during processing | `./local/tmp/` | Used for intermediate file storage during data processing |

## Workflow Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `ATLAN_WORKFLOW_HOST` | Host address for the Temporal server | `localhost` | Connection to Temporal orchestration engine |
| `ATLAN_WORKFLOW_PORT` | Port number for the Temporal server | `7233` | Connection port for Temporal server |
| `ATLAN_WORKFLOW_NAMESPACE` | Namespace for Temporal workflows | `default` | Isolates workflows between different environments |
| `ATLAN_WORKFLOW_UI_HOST` | Host address for the Temporal UI | `localhost` | Access to Temporal Web UI for monitoring |
| `ATLAN_WORKFLOW_UI_PORT` | Port number for the Temporal UI | `8233` | Port for Temporal Web UI |
| `ATLAN_WORKFLOW_MAX_TIMEOUT_HOURS` | Maximum timeout duration for workflows (in hours) | `1` | Prevents runaway workflows from consuming resources |
| `ATLAN_MAX_CONCURRENT_ACTIVITIES` | Maximum number of activities that can run concurrently | `5` | Controls resource usage and prevents overwhelming target systems |
| `ATLAN_HEARTBEAT_TIMEOUT_SECONDS` | Timeout duration for activity heartbeats (in seconds) | `300` | Detects stuck activities and enables recovery |
| `ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS` | Maximum duration an activity can run before timing out (in seconds) | `7200` | Prevents activities from running indefinitely |
| `ATLAN_AUTH_ENABLED` | Whether to enable authentication for Temporal workflows | `false` | Used in production deployments with secure Temporal clusters |
| `ATLAN_DEPLOYMENT_SECRET_PATH` | Path to deployment secrets in secret store | `ATLAN_DEPLOYMENT_SECRETS` | Contains authentication credentials for production |

## SQL Client Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `ATLAN_SQL_USE_SERVER_SIDE_CURSOR` | Whether to use server-side cursors for SQL operations | `true` | Reduces memory usage for large result sets by streaming data |

## DAPR Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `STATE_STORE_NAME` | Name of the state store component in DAPR | `statestore` | Persistent state storage for workflow checkpoints |
| `SECRET_STORE_NAME` | Name of the secret store component in DAPR | `secretstore` | Secure storage for credentials and API keys |
| `DEPLOYMENT_OBJECT_STORE_NAME` | Name of the deployment object store component in DAPR | `objectstore` | Storage for workflow outputs and artifacts |
| `UPSTREAM_OBJECT_STORE_NAME` | Name of the upstream object store component in DAPR | `objectstore` | Storage for uploading data to Atlan platform |
| `EVENT_STORE_NAME` | Name of the pubsub component in DAPR | `eventstore` | Event publishing and subscription |
| `DEPLOYMENT_SECRET_STORE_NAME` | Name of the deployment secret store component in DAPR | `deployment-secret-store` | Environment-specific secrets storage |
| `DAPR_MAX_GRPC_MESSAGE_LENGTH` | Maximum gRPC message length in bytes for Dapr client | `16777216` (16MB) | Controls maximum size of data exchanged with DAPR components |
| `ENABLE_ATLAN_UPLOAD` | Whether to enable Atlan storage upload | `false` | Enables uploading processed data to Atlan platform |

## Observability Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `ATLAN_ENABLE_HIVE_PARTITIONING` | Whether to enable Hive partitioning for observability data | `true` | Organizes data by date for efficient querying and cleanup |
| `ATLAN_ENABLE_OBSERVABILITY_DAPR_SINK` | Whether to enable Dapr sink for observability data | `false` | Routes observability data through DAPR components |

## Logging Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `LOG_LEVEL` | Log level for the application (DEBUG, INFO, WARNING, ERROR, CRITICAL) | `INFO` | Controls verbosity of application logs |
| `ATLAN_LOG_BATCH_SIZE` | Number of log records to buffer before writing to parquet file | `100` | Optimizes I/O by batching log writes |
| `ATLAN_LOG_FLUSH_INTERVAL_SECONDS` | Time interval (in seconds) to flush logs to parquet file | `10` | Ensures logs are persisted regularly |
| `ATLAN_LOG_RETENTION_DAYS` | Number of days to retain log records before automatic cleanup | `30` | Manages storage usage by removing old logs |
| `ATLAN_LOG_CLEANUP_ENABLED` | Whether to enable automatic cleanup of old logs | `false` | Enables automatic log file cleanup |
| `ATLAN_LOG_FILE_NAME` | Name of the parquet file used for log storage | `log.parquet` | Customizes log file naming |

## Metrics Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `ATLAN_ENABLE_OTLP_METRICS` | Whether to enable OpenTelemetry metrics export | `false` | Enables metrics collection for monitoring and alerting |
| `ATLAN_METRICS_BATCH_SIZE` | Number of metric records to buffer before writing to parquet file | `100` | Optimizes I/O by batching metric writes |
| `ATLAN_METRICS_FLUSH_INTERVAL_SECONDS` | Time interval (in seconds) to flush metrics to parquet file | `10` | Controls frequency of metric persistence |
| `ATLAN_METRICS_RETENTION_DAYS` | Number of days to retain metric records before automatic cleanup | `30` | Manages storage usage for historical metrics |
| `ATLAN_METRICS_CLEANUP_ENABLED` | Whether to enable automatic cleanup of old metrics | `false` | Enables automatic metric file cleanup |

## Traces Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `ATLAN_ENABLE_OTLP_TRACES` | Whether to enable OpenTelemetry traces export | `false` | Enables distributed tracing for performance monitoring |
| `ATLAN_TRACES_BATCH_SIZE` | Number of trace records to buffer before writing to parquet file | `100` | Optimizes I/O by batching trace writes |
| `ATLAN_TRACES_FLUSH_INTERVAL_SECONDS` | Time interval (in seconds) to flush traces to parquet file | `5` | Ensures timely trace persistence for debugging |
| `ATLAN_TRACES_RETENTION_DAYS` | Number of days to retain trace records before automatic cleanup | `30` | Manages storage usage for historical traces |
| `ATLAN_TRACES_CLEANUP_ENABLED` | Whether to enable automatic cleanup of old traces | `true` | Enables automatic trace file cleanup to prevent disk overflow |

## OpenTelemetry Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `OTEL_SERVICE_NAME` | Service name for OpenTelemetry | `atlan-application-sdk` | Identifies the service in telemetry data |
| `OTEL_SERVICE_VERSION` | Service version for OpenTelemetry | `0.1.0` | Tracks service version in telemetry data |
| `OTEL_RESOURCE_ATTRIBUTES` | Additional resource attributes for OpenTelemetry | `""` | Custom metadata for telemetry resources |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Endpoint for the OpenTelemetry collector | `http://localhost:4317` | Target for telemetry data export |
| `ENABLE_OTLP_LOGS` | Whether to enable OpenTelemetry log export | `false` | Enables structured log export to OTLP |
| `OTEL_WF_NODE_NAME` | Node name for workflow telemetry | `""` | Identifies workflow execution node |
| `OTEL_EXPORTER_TIMEOUT_SECONDS` | Timeout for OpenTelemetry exporters in seconds | `30` | Prevents hanging export operations |
| `OTEL_BATCH_DELAY_MS` | Delay between batch exports in milliseconds | `5000` | Controls export frequency to reduce overhead |
| `OTEL_BATCH_SIZE` | Maximum size of export batches | `512` | Optimizes export performance |
| `OTEL_QUEUE_SIZE` | Maximum size of the export queue | `2048` | Prevents memory overflow from queued telemetry |

## AWS Configuration

| Environment Variable | Description | Default Value | Use Case |
|---------------------|-------------|---------------|----------|
| `AWS_SESSION_NAME` | AWS Session Name for temporary credentials | `temp-session` | Used when assuming AWS roles for object store access |

## Path Templates and Constants

| Constant | Description | Use Case |
|----------|-------------|----------|
| `WORKFLOW_OUTPUT_PATH_TEMPLATE` | Template for workflow output paths: `artifacts/apps/{application_name}/workflows/{workflow_id}/{run_id}` | Organizing workflow outputs in object storage |
| `STATE_STORE_PATH_TEMPLATE` | Template for state store paths: `persistent-artifacts/apps/{application_name}/{state_type}/{id}/config.json` | Organizing persistent state data |
| `OBSERVABILITY_DIR` | Directory for observability data: `artifacts/apps/{application_name}/observability` | Storing logs, metrics, and traces |

## Common Configuration Patterns

### Local Development
For local development, most defaults work out of the box. Key configurations to consider:
- `ATLAN_APPLICATION_NAME`: Set to a descriptive name for your application
- `ATLAN_TENANT_ID`: Set to identify your development environment
- `LOG_LEVEL`: Set to `DEBUG` for detailed logging during development

### Production Deployment
For production deployments, consider these essential configurations:
- `ATLAN_AUTH_ENABLED=true`: Enable authentication for Temporal
- `ENABLE_ATLAN_UPLOAD=true`: Enable data upload to Atlan platform
- `ATLAN_DEPLOYMENT_SECRETS`: Configure authentication secrets
- `ATLAN_WORKFLOW_HOST` and `ATLAN_WORKFLOW_PORT`: Point to production Temporal cluster
- Storage configuration: Set `DEPLOYMENT_OBJECT_STORE_NAME` and `UPSTREAM_OBJECT_STORE_NAME`

### Performance Tuning
- `ATLAN_MAX_CONCURRENT_ACTIVITIES`: Adjust based on target system capacity
- `DAPR_MAX_GRPC_MESSAGE_LENGTH`: Increase for large data processing
- Batch sizes: Adjust `*_BATCH_SIZE` variables based on memory and performance requirements
- Timeout values: Adjust `ATLAN_HEARTBEAT_TIMEOUT_SECONDS` and `ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS` based on workload characteristics

### Observability Setup
- Enable telemetry: Set `ATLAN_ENABLE_OTLP_*` variables to `true`
- Configure retention: Adjust `*_RETENTION_DAYS` based on compliance requirements
- Set up cleanup: Enable `*_CLEANUP_ENABLED` to manage storage usage

## Note

Most configuration options have sensible defaults, but can be overridden by setting the corresponding environment variables. You can set these variables either in your environment or by creating a `.env` file in your project root.

Refer to the `.env.example` file in the repository for a complete example of environment variable configuration.
