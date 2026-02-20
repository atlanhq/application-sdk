# Dual Export Test Setup

Tests the SDK's dual OTEL export functionality with two separate collectors.

## Architecture

```
                              ┌─────────────────────────────┐
                              │   Application (SDK)         │
                              │                             │
                              │ OTEL_EXPORTER_OTLP_ENDPOINT │
                              │ = localhost:4317            │
                              │                             │
                              │ OTEL_WORKFLOW_LOGS_ENDPOINT │
                              │ = localhost:5317            │
                              └──────────┬──────────────────┘
                                         │
                    ┌────────────────────┼────────────────────┐
                    │                    │                    │
                    ▼                    │                    ▼
    ┌───────────────────────────┐       │    ┌───────────────────────────┐
    │  Primary Collector        │       │    │  Workflow Logs Collector  │
    │  (port 4317)              │       │    │  (port 5317)              │
    │                           │       │    │                           │
    │  Simulates: DaemonSet     │       │    │  Simulates: Tenant-level  │
    │  Export: debug (console)  │       │    │  Export: awss3 (MinIO)    │
    └───────────────────────────┘       │    └─────────────┬─────────────┘
                                        │                  │
                                        │                  ▼
                                        │    ┌───────────────────────────┐
                                        │    │  MinIO (S3)               │
                                        │    │  Bucket: workflow-logs    │
                                        │    │  Console: localhost:9001  │
                                        │    └───────────────────────────┘
```

## Quick Start

```bash
# 1. Start the collectors and MinIO
cd poc/dual-export-test
docker-compose up -d

# 2. Verify services are running
docker-compose ps

# 3. Run the test script (from repo root)
cd /path/to/application-sdk
source .venv/bin/activate
python poc/dual-export-test/test_dual_export.py

# 4. Check both collectors received logs
docker logs primary-collector
docker logs workflow-logs-collector

# 5. Check MinIO for S3 files
# Open http://localhost:9001 (login: minioadmin/minioadmin)
# Browse to 'workflow-logs' bucket
```

## Manual Testing with MSSQL App

```bash
# 1. Start collectors (if not already running)
cd application-sdk/poc/dual-export-test
docker-compose up -d

# 2. Go to mssql app
cd atlan-mssql-app
source .venv/bin/activate

# 3. Set environment variables
export ENABLE_OTLP_LOGS=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_WORKFLOW_LOGS_ENDPOINT=http://localhost:5317

# 4. Run your workflow/app
# ... your normal commands ...

# 5. Check logs in collectors
docker logs primary-collector -f
docker logs workflow-logs-collector -f
```

## Cleanup

```bash
cd poc/dual-export-test
docker-compose down -v
```

## Port Mapping

| Service | Port | Description |
|---------|------|-------------|
| Primary Collector | 4317 | OTLP gRPC (simulates DaemonSet) |
| Workflow Logs Collector | 5317 | OTLP gRPC (simulates tenant-level) |
| MinIO API | 9000 | S3-compatible API |
| MinIO Console | 9001 | Web UI (minioadmin/minioadmin) |
