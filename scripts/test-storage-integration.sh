#!/usr/bin/env bash
#
# Storage integration tests — spins up MinIO, runs tests, tears down.
#
# Usage:
#   ./scripts/test-storage-integration.sh          # run all storage integration tests
#   ./scripts/test-storage-integration.sh -k upload # run only tests matching "upload"
#   poe test-storage-integration                    # via poe task
#
set -euo pipefail

CONTAINER_NAME="minio-storage-test"
MINIO_PORT=9000
MINIO_CONSOLE_PORT=9001
MINIO_USER="minioadmin"
MINIO_PASS="minioadmin"
BUCKET="test-bucket"
TEST_PATH="tests/integration/storage/"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

cleanup() {
    echo -e "\n${YELLOW}Tearing down MinIO...${NC}"
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    echo -e "${GREEN}Cleanup complete.${NC}"
}

# Always clean up, even on failure
trap cleanup EXIT

# ---------- Pre-flight checks ----------
if ! command -v docker &>/dev/null; then
    echo -e "${RED}Error: docker is not installed or not in PATH.${NC}"
    exit 1
fi

if ! docker info &>/dev/null; then
    echo -e "${RED}Error: Docker daemon is not running.${NC}"
    exit 1
fi

# ---------- Remove stale container if exists ----------
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# ---------- Start MinIO ----------
echo -e "${YELLOW}Starting MinIO on :${MINIO_PORT}...${NC}"
docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${MINIO_PORT}:9000" \
    -p "${MINIO_CONSOLE_PORT}:9001" \
    -e "MINIO_ROOT_USER=${MINIO_USER}" \
    -e "MINIO_ROOT_PASSWORD=${MINIO_PASS}" \
    minio/minio server /data --console-address ":9001" \
    >/dev/null

# ---------- Wait for MinIO to be ready ----------
echo -n "Waiting for MinIO to be ready"
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${MINIO_PORT}/minio/health/ready" >/dev/null 2>&1; then
        echo -e " ${GREEN}ready!${NC}"
        break
    fi
    echo -n "."
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo -e "\n${RED}MinIO failed to start within 30s.${NC}"
        docker logs "$CONTAINER_NAME" 2>&1 | tail -20
        exit 1
    fi
done

# ---------- Create bucket ----------
echo -e "${YELLOW}Creating bucket '${BUCKET}'...${NC}"
docker run --rm --network host --entrypoint sh minio/mc -c \
    "mc alias set local http://localhost:${MINIO_PORT} ${MINIO_USER} ${MINIO_PASS} && mc mb --ignore-existing local/${BUCKET}" \
    >/dev/null 2>&1

echo -e "${GREEN}MinIO ready with bucket '${BUCKET}'.${NC}"

# ---------- Run tests ----------
echo -e "\n${YELLOW}Running storage integration tests...${NC}\n"

DAPR_COMPONENTS_PATH=/nonexistent \
AWS_ENDPOINT_URL="http://localhost:${MINIO_PORT}" \
AWS_ACCESS_KEY_ID="${MINIO_USER}" \
AWS_SECRET_ACCESS_KEY="${MINIO_PASS}" \
AWS_DEFAULT_REGION="us-east-1" \
AWS_ALLOW_HTTP="true" \
ATLAN_OBJECT_STORE_BUCKET="${BUCKET}" \
uv run pytest "${TEST_PATH}" -v "$@"

echo -e "\n${GREEN}All storage integration tests passed!${NC}"
