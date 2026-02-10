# Kubernetes Manifests for Workflow Logs Observability POC

This directory contains standalone K8s manifests for deploying the workflow logs observability components to a tenant cluster.

## Components

1. **workflow-logs-collector**: OTEL collector that receives logs from SDK and exports to S3 + observability app
2. **observability-app**: FastAPI service for live streaming and Iceberg querying

## Prerequisites

- kubectl configured to the target cluster
- Access to platform-temporal namespace
- For Iceberg queries: Polaris catalog credentials

## Quick Deployment (POC)

```bash
# 1. Deploy the workflow-logs-collector
kubectl apply -f workflow-logs-collector.yaml

# 2. Deploy the observability app (customize env vars first!)
kubectl apply -f observability-app.yaml

# 3. Verify deployments
kubectl get pods -n platform-temporal | grep -E "(workflow-logs|observability)"
kubectl get svc -n platform-temporal | grep -E "(workflow-logs|observability)"
```

## Configuration

### Workflow Logs Collector

The collector is pre-configured for AWS S3. Customize these values in `workflow-logs-collector.yaml`:
- `S3_BUCKET_REGION`: Your AWS region (e.g., us-east-1)
- `S3_BUCKET`: Your S3 bucket name
- `S3_PREFIX`: Path prefix for logs (default: workflow-logs/logs)

### Observability App

Configure Polaris/Iceberg connection in `observability-app.yaml`:
- `POLARIS_CATALOG_URI`: Polaris catalog endpoint
- `POLARIS_CRED`: Polaris credentials (from secret)
- `NAMESPACE`: Iceberg namespace for workflow logs table

## Access

Port-forward to access the observability app locally:

```bash
kubectl port-forward -n platform-temporal svc/observability-app 8081:8080
```

Then access:
- Health check: http://localhost:8081/health
- Live streaming: http://localhost:8081/sse/logs?trace_id=YOUR_TRACE_ID
- Historical query: http://localhost:8081/api/logs?trace_id=YOUR_TRACE_ID

## SDK Configuration

Configure the MSSQL app (or any SDK app) to send logs to the collector:

```yaml
env:
- name: OTEL_WORKFLOW_LOGS_ENDPOINT
  value: "workflow-logs-collector.platform-temporal.svc.cluster.local:4317"
```

## Cleanup

```bash
kubectl delete -f observability-app.yaml
kubectl delete -f workflow-logs-collector.yaml
```
