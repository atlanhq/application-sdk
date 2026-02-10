#!/bin/bash
# Script to extract Polaris/MDLH config from existing tenant deployments
# Run this while connected to the target cluster

set -e

echo "=== Searching for deployments with Polaris config ==="
echo ""

# Look for context-store or mdlh related deployments
NAMESPACES=("default" "monitoring" "platform-temporal")

for ns in "${NAMESPACES[@]}"; do
    echo "Checking namespace: $ns"
    kubectl get deployments -n "$ns" -o name 2>/dev/null | while read deploy; do
        if kubectl get "$deploy" -n "$ns" -o yaml 2>/dev/null | grep -q -E "(POLARIS|polaris|MDLH|mdlh)" ; then
            echo "  Found Polaris config in: $deploy"
        fi
    done
done

echo ""
echo "=== Extracting Polaris env vars from context-store (if exists) ==="
echo ""

# Try to get config from context-store
CONTEXT_STORE=$(kubectl get deployments -A -o name 2>/dev/null | grep -i context-store | head -1)
if [ -n "$CONTEXT_STORE" ]; then
    NS=$(kubectl get deployments -A 2>/dev/null | grep -i context-store | head -1 | awk '{print $1}')
    DEPLOY_NAME=$(echo "$CONTEXT_STORE" | sed 's|deployment.apps/||')
    echo "Found context-store: $DEPLOY_NAME in namespace: $NS"
    echo ""
    echo "Extracting env vars:"
    kubectl get deployment "$DEPLOY_NAME" -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].env[*]}' 2>/dev/null | \
        python3 -c "
import sys, json
try:
    data = sys.stdin.read()
    # Parse the JSON array
    items = json.loads('[' + data.replace('} {', '},{') + ']')
    for item in items:
        name = item.get('name', '')
        if any(x in name.lower() for x in ['polaris', 'mdlh', 'catalog', 'warehouse', 'iceberg']):
            value = item.get('value', item.get('valueFrom', 'FROM_SECRET'))
            print(f'{name}={value}')
except:
    print('Error parsing env vars. Use kubectl describe deployment to view.')
"
else
    echo "context-store deployment not found."
fi

echo ""
echo "=== S3 Bucket Info ==="
kubectl get configmap -n default -o yaml 2>/dev/null | grep -E "s3_bucket|S3_BUCKET|bucket" | head -5 || echo "No S3 config found in configmaps"

echo ""
echo "=== Manual extraction command ==="
echo "kubectl get deployment <DEPLOYMENT_NAME> -n <NAMESPACE> -o jsonpath='{.spec.template.spec.containers[0].env}' | jq ."
