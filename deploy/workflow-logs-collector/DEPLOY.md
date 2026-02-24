# Workflow Logs Phase 1: Deployment Runbook

## Prerequisites

- kubectl access to tenant cluster
- Kafka brokers running in `kafka` namespace (kafka-{0,1,2}.kafka-headless:9092)
- `platform-temporal` namespace exists
- New connector images built from `feat/otel-kafka-export` branches

## Deployment Order

### 1. Create Kafka Topic

```bash
kubectl exec -it kafka-0 -n kafka -- kafka-topics.sh \
  --create \
  --bootstrap-server kafka-headless.kafka.svc.cluster.local:9092 \
  --topic workflow-logs \
  --partitions 6 \
  --replication-factor 3 \
  --config retention.ms=3600000 \
  --config retention.bytes=-1 \
  --config segment.ms=300000 \
  --config cleanup.policy=delete \
  --config max.message.bytes=1048576 \
  --config compression.type=lz4
```

Verify:
```bash
kubectl exec kafka-0 -n kafka -- kafka-topics.sh \
  --describe --topic workflow-logs \
  --bootstrap-server kafka-headless.kafka.svc.cluster.local:9092
```

### 2. Deploy Tenant OTel Collector

```bash
kubectl apply -f workflow-logs-collector.yaml
```

Verify collector is healthy:
```bash
kubectl get pods -n platform-temporal -l app=workflow-logs-collector
kubectl logs -n platform-temporal -l app=workflow-logs-collector --tail=20
```

Verify health endpoint:
```bash
kubectl exec -n platform-temporal deploy/workflow-logs-collector -- wget -qO- http://localhost:13133/
```

### 3. Verify Collector -> Kafka Connectivity

Start a consumer to watch for messages:
```bash
kubectl exec kafka-0 -n kafka -- kafka-console-consumer.sh \
  --bootstrap-server kafka-headless.kafka.svc.cluster.local:9092 \
  --topic workflow-logs --from-beginning --max-messages 5 --timeout-ms 30000
```

### 4. Update Connector Helm Values

In the `atlanhq/atlan` repo, update connector worker environment variables:

```yaml
# Add to connector worker env vars (both redshift and mssql)
ENABLE_OTLP_LOGS: "true"
ENABLE_WORKFLOW_LOGS_EXPORT: "true"
OTEL_WORKFLOW_LOGS_ENDPOINT: "workflow-logs-collector.platform-temporal.svc.cluster.local:4317"
```

### 5. Update Connector Image Tags

Update the image tags in Helm values to the new builds from `feat/otel-kafka-export`:

- `ghcr.io/atlanhq/atlan-redshift-app-feat-otel-kafka-export:latest`
- `ghcr.io/atlanhq/atlan-mssql-app-feat-otel-kafka-export:latest`

Or use the specific version tags from the CI build output.

### 6. Trigger a Test Workflow

Run a redshift or mssql extraction workflow. Then verify:

**SDK dual export active** (check connector pod logs):
```bash
kubectl logs -n platform-temporal <connector-pod> | grep -i "workflow.logs\|dual.*export\|secondary.*exporter"
```

**Kafka has data:**
```bash
kubectl exec kafka-0 -n kafka -- kafka-console-consumer.sh \
  --bootstrap-server kafka-headless.kafka.svc.cluster.local:9092 \
  --topic workflow-logs --from-beginning --max-messages 5 --timeout-ms 30000
```

**Verify OTLP JSON structure** (messages should contain):
```json
{
  "resourceLogs": [{
    "resource": {
      "attributes": [
        {"key": "application.name", "value": {"stringValue": "redshift"}},
        {"key": "workflow_id", "value": {"stringValue": "..."}},
        {"key": "trace_id", "value": {"stringValue": "..."}}
      ]
    },
    "scopeLogs": [{"logRecords": [...]}]
  }]
}
```

**Primary path unaffected:**
Confirm existing host DaemonSet -> central ClickHouse dashboards still show logs.

**Topic health:**
```bash
kubectl exec kafka-0 -n kafka -- kafka-topics.sh \
  --describe --topic workflow-logs \
  --bootstrap-server kafka-headless.kafka.svc.cluster.local:9092
```

## Rollback

To disable without removing infrastructure:
```yaml
ENABLE_WORKFLOW_LOGS_EXPORT: "false"
```

To remove entirely:
```bash
kubectl delete -f workflow-logs-collector.yaml
kubectl exec kafka-0 -n kafka -- kafka-topics.sh \
  --delete --topic workflow-logs \
  --bootstrap-server kafka-headless.kafka.svc.cluster.local:9092
```

## Image References

| Repo | Branch | Image |
|------|--------|-------|
| atlanhq/application-sdk | feat/otel-kafka-export | (SDK, not containerized) |
| atlanhq/atlan-redshift-app | feat/otel-kafka-export | ghcr.io/atlanhq/atlan-redshift-app-feat-otel-kafka-export |
| atlanhq/atlan-mssql-app | feat/otel-kafka-export | ghcr.io/atlanhq/atlan-mssql-app-feat-otel-kafka-export |
| otel-contrib (upstream) | - | otel/opentelemetry-collector-contrib:0.129.1 |
