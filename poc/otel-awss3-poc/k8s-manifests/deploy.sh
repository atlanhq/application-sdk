#!/bin/bash
# Deploy workflow logs observability POC to tenant
# BLDX-361
#
# Usage:
#   ./deploy.sh [--s3-bucket BUCKET] [--s3-region REGION] [--polaris-uri URI] [--polaris-cred CRED]
#
# Or set environment variables:
#   S3_BUCKET=your-bucket S3_REGION=us-east-1 ./deploy.sh

set -e

# Configuration
NAMESPACE="platform-temporal"
S3_BUCKET="${S3_BUCKET:-}"
S3_REGION="${S3_REGION:-us-east-1}"
POLARIS_URI="${POLARIS_URI:-}"
POLARIS_CRED="${POLARIS_CRED:-}"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --s3-bucket) S3_BUCKET="$2"; shift 2 ;;
        --s3-region) S3_REGION="$2"; shift 2 ;;
        --polaris-uri) POLARIS_URI="$2"; shift 2 ;;
        --polaris-cred) POLARIS_CRED="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== Workflow Logs Observability POC Deployment ==="
echo ""
echo "Configuration:"
echo "  Namespace: $NAMESPACE"
echo "  S3 Bucket: ${S3_BUCKET:-NOT SET}"
echo "  S3 Region: $S3_REGION"
echo "  Polaris URI: ${POLARIS_URI:-NOT SET (Iceberg queries disabled)}"
echo ""

# Check required config
if [ -z "$S3_BUCKET" ]; then
    echo "ERROR: S3_BUCKET is required. Set it via --s3-bucket or S3_BUCKET env var."
    echo ""
    echo "Example: ./deploy.sh --s3-bucket atlan-vcluster-aplon95p04-5049w2n0ijcf"
    exit 1
fi

# Create temp directory for processed manifests
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

# Process workflow-logs-collector.yaml
echo "Processing workflow-logs-collector.yaml..."
sed -e "s|REPLACE_WITH_YOUR_BUCKET|$S3_BUCKET|g" \
    -e "s|endpoint: https://s3\.us-east-1\.amazonaws\.com|endpoint: https://s3.$S3_REGION.amazonaws.com|g" \
    -e "s|region: us-east-1|region: $S3_REGION|g" \
    workflow-logs-collector.yaml > "$TMPDIR/workflow-logs-collector.yaml"

# Process observability-app.yaml
echo "Processing observability-app.yaml..."
cp observability-app.yaml "$TMPDIR/observability-app.yaml"

if [ -n "$POLARIS_URI" ] && [ -n "$POLARIS_CRED" ]; then
    sed -i.bak \
        -e "s|REPLACE_WITH_POLARIS_URI|$POLARIS_URI|g" \
        -e "s|REPLACE_WITH_POLARIS_CRED|$POLARIS_CRED|g" \
        "$TMPDIR/observability-app.yaml"
    echo "  Polaris config applied"
else
    echo "  WARNING: Polaris config not set. Iceberg queries will be disabled."
fi

# Ensure namespace exists
echo ""
echo "Ensuring namespace $NAMESPACE exists..."
kubectl create namespace "$NAMESPACE" 2>/dev/null || true

# Deploy collector
echo ""
echo "Deploying workflow-logs-collector..."
kubectl apply -f "$TMPDIR/workflow-logs-collector.yaml"

# Deploy observability app
echo ""
echo "Deploying observability-app..."
kubectl apply -f "$TMPDIR/observability-app.yaml"

# Wait for deployments
echo ""
echo "Waiting for deployments to be ready..."
kubectl wait --for=condition=available deployment/workflow-logs-collector -n "$NAMESPACE" --timeout=120s || true
kubectl wait --for=condition=available deployment/observability-app -n "$NAMESPACE" --timeout=120s || true

# Show status
echo ""
echo "=== Deployment Status ==="
kubectl get pods -n "$NAMESPACE" -l 'app in (workflow-logs-collector,observability-app)'
echo ""
kubectl get svc -n "$NAMESPACE" -l 'app in (workflow-logs-collector,observability-app)'

echo ""
echo "=== Access Instructions ==="
echo ""
echo "1. Port-forward to access the observability app:"
echo "   kubectl port-forward -n $NAMESPACE svc/observability-app 8081:8080"
echo ""
echo "2. Test health endpoint:"
echo "   curl http://localhost:8081/health"
echo ""
echo "3. Configure SDK apps to send logs to collector:"
echo "   OTEL_WORKFLOW_LOGS_ENDPOINT=workflow-logs-collector.$NAMESPACE.svc.cluster.local:4317"
echo ""
echo "=== Done ==="
