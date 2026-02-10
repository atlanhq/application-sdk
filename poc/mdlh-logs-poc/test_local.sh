#!/bin/bash
# Test script for local MinIO + Iceberg flow
# Prerequisites: docker-compose running in poc/dual-export-test/

set -e

# MinIO settings (override S3 settings)
export S3_BUCKET=artifacts
export S3_PREFIX=apps/workflow-logs
export S3_ENDPOINT=http://localhost:9000
export S3_FORCE_PATH_STYLE=true
export AWS_ACCESS_KEY_ID=minioadmin
export AWS_SECRET_ACCESS_KEY=minioadmin
export AWS_SESSION_TOKEN=""  # Empty for MinIO

echo "=== Testing Local MinIO -> Iceberg Flow ==="
echo ""
echo "S3 Config:"
echo "  Bucket: $S3_BUCKET"
echo "  Prefix: $S3_PREFIX"
echo "  Endpoint: $S3_ENDPOINT"
echo ""

# Check MinIO is running
echo "Checking MinIO connectivity..."
if ! curl -s http://localhost:9000/minio/health/live > /dev/null; then
    echo "ERROR: MinIO not reachable at localhost:9000"
    echo "Run: cd ../dual-export-test && docker-compose up -d"
    exit 1
fi
echo "MinIO is running."
echo ""

# List files in MinIO
echo "Listing files in MinIO..."
docker run --rm --network dual-export-test-network \
    -e MC_HOST_myminio=http://minioadmin:minioadmin@minio:9000 \
    minio/mc ls -r myminio/$S3_BUCKET/$S3_PREFIX/ 2>/dev/null | head -10
echo ""

# Run ingestion
echo "Running ingestion..."
cd "$(dirname "$0")"
python3 -m ingestion.s3_to_iceberg

echo ""
echo "=== Ingestion Complete ==="
