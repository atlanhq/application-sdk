# OTEL AWSS3 POC - Workflow Logs Observability

End-to-end proof of concept for workflow log observability using:
- OpenTelemetry Collector with awss3 exporter (Hive partitions, OTLP JSON)
- MinIO for S3-compatible storage
- FastAPI observability app for live streaming (SSE) and historical queries
- Iceberg integration for data lake storage

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│  Docker Compose                                                                      │
│                                                                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │ Mock Workers (3 instances)                                           │           │
│  │   worker-snowflake: workflow_id=wf-snowflake-001                    │           │
│  │   worker-mssql:     workflow_id=wf-mssql-002                        │           │
│  │   worker-postgres:  workflow_id=wf-postgres-003                     │           │
│  └──────────────────────────┬───────────────────────────────────────────┘           │
│                             │ OTLP/gRPC                                             │
│                             ▼                                                        │
│  ┌──────────────────────────────────────────────────────────────────────┐           │
│  │ OTEL Collector (otel/opentelemetry-collector-contrib)               │           │
│  │                                                                      │           │
│  │ Pipelines:                                                           │           │
│  │   logs/archive → batch(10s) → awss3 (OTLP JSON) ────┐               │           │
│  │   logs/live ───→ batch(2s) ──→ otlphttp ───────────┐│               │           │
│  └────────────────────────────────────────────────────┼┼────────────────┘           │
│                                                       ││                            │
│                    ┌──────────────────────────────────┘│                            │
│                    │                                   │                            │
│                    ▼                                   ▼                            │
│  ┌─────────────────────────────────┐   ┌────────────────────────────────┐          │
│  │ MinIO (S3)                      │   │ Observability App (FastAPI)    │          │
│  │                                 │   │                                │          │
│  │ workflow-logs/                  │   │ POST /v1/logs  (OTLP receiver) │          │
│  │   year=2026/month=02/...       │   │ GET /sse/logs  (live stream)   │          │
│  │     {uuid}.json                 │   │ GET /api/logs  (Iceberg query) │          │
│  └─────────────────────────────────┘   └────────────────────────────────┘          │
│                                                                                      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

```bash
# Start all services
make up

# In another terminal, watch the logs
make logs

# List files in S3
make s3-ls

# Run tests
make test

# Stop services
make down
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| MinIO | 9000, 9001 | S3-compatible storage (console at 9001) |
| OTEL Collector | 4317, 4318 | Receives OTLP logs, exports to S3 and app |
| Observability App | 8081 | Live streaming and query API |
| Workers (3x) | - | Mock workflow workers sending logs |

## API Endpoints

### Observability App (http://localhost:8081)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/v1/logs` | POST | OTLP log receiver |
| `/sse/logs?workflow_id=X` | GET | SSE stream for workflow |
| `/api/logs?workflow_id=X` | GET | Query logs from buffer or Iceberg |
| `/api/workflows` | GET | List active workflows |

### Examples

```bash
# Health check
curl http://localhost:8081/health

# List workflows
curl http://localhost:8081/api/workflows

# Query logs for a workflow
curl "http://localhost:8081/api/logs?workflow_id=wf-snowflake-001&source=buffer"

# Stream logs (SSE)
curl -N "http://localhost:8081/sse/logs?workflow_id=wf-snowflake-001"
```

## S3 Structure

Files are stored with Hive partitions:
```
workflow-logs/
└── logs/
    └── year=2026/
        └── month=02/
            └── day=03/
                └── hour=10/
                    ├── <uuid>.json
                    └── <uuid>.json
```

## OTLP JSON Format

```json
{
  "resourceLogs": [{
    "resource": {
      "attributes": [
        {"key": "service.name", "value": {"stringValue": "snowflake-extractor"}}
      ]
    },
    "scopeLogs": [{
      "logRecords": [{
        "timeUnixNano": "1706961234000000000",
        "severityText": "INFO",
        "body": {"stringValue": "Processing started"},
        "attributes": [
          {"key": "workflow_id", "value": {"stringValue": "wf-snowflake-001"}},
          {"key": "trace_id", "value": {"stringValue": "wf-snowflake-001"}}
        ]
      }]
    }]
  }]
}
```

## Testing

```bash
# Run all tests
make test

# Individual test suites
make test-s3       # S3 structure verification
make test-otlp     # OTLP JSON format validation
make test-sse      # Live streaming test
make test-iceberg  # Iceberg query test (requires Polaris)
```

## Iceberg Integration

For historical queries via Iceberg, set these environment variables:

```bash
export POLARIS_CATALOG_URI="https://your-polaris-endpoint/api/catalog"
export POLARIS_CRED="your-credentials"
export CATALOG_NAME="context_store"
export WAREHOUSE_NAME="context_store"
export NAMESPACE="Workflow_Log_Test"
```

Then run ingestion:
```bash
make ingest
```

## Troubleshooting

### No files in S3
- Check collector logs: `make logs-col`
- Verify workers are running: `make status`
- Check MinIO health: `curl http://localhost:9000/minio/health/live`

### SSE not receiving logs
- Check observability app logs: `make logs-app`
- Verify workflows are active: `curl http://localhost:8081/api/workflows`

### Collector not starting
- Check for port conflicts on 4317, 4318
- Verify MinIO is healthy before collector starts

## Files

```
poc/otel-awss3-poc/
├── docker-compose.yaml          # All services
├── collector-config.yaml        # OTEL collector configuration
├── Makefile                     # Convenience commands
├── README.md                    # This file
│
├── observability-app/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py                  # FastAPI app
│
├── mock-worker/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── worker.py                # Mock workflow worker
│
├── ingestion/
│   └── Dockerfile               # For S3 -> Iceberg ingestion
│
└── tests/
    ├── requirements.txt
    ├── test_s3_structure.py
    ├── test_otlp_json_format.py
    ├── test_live_streaming.py
    ├── test_iceberg_query.py
    └── run_all_tests.py
```

## Pluggable Parser System

The ingestion uses a pluggable parser system to support multiple log formats:

```python
from parsers.base import get_default_registry

registry = get_default_registry()
parser = registry.get_parser("file.json")  # Returns OTLPJsonParser
parser = registry.get_parser("file.parquet")  # Returns ParquetParser
```

Located in `poc/mdlh-logs-poc/ingestion/parsers/`.
