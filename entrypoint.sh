#!/bin/sh
# Graceful shutdown entrypoint for Atlan Application SDK containers
set -e

# =============================================================================
# Graceful Shutdown Wrapper for Atlan Application SDK
#
# Starts the Dapr sidecar (daprd) directly, waits for it to be ready,
# then launches the application. Handles graceful shutdown on SIGTERM.
# =============================================================================

DAPRD_PID=""
APP_PID=""

term_handler() {
    echo "[entrypoint] Received SIGTERM, initiating graceful shutdown..."

    if [ -n "$APP_PID" ]; then
        echo "[entrypoint] Forwarding SIGTERM to application (PID: $APP_PID)..."
        kill -TERM "$APP_PID" 2>/dev/null || true
        wait "$APP_PID" 2>/dev/null || true
        echo "[entrypoint] Application shutdown complete"
    fi

    if [ -n "$DAPRD_PID" ]; then
        echo "[entrypoint] Stopping Dapr sidecar (PID: $DAPRD_PID)..."
        kill -TERM "$DAPRD_PID" 2>/dev/null || true
    fi

    exit 0
}

trap term_handler SIGTERM SIGINT

# Start daprd sidecar in the background
daprd \
    --log-level "$DAPR_LOG_LEVEL" \
    --app-id "$DAPR_APP_ID" \
    --scheduler-host-address '' \
    --placement-host-address '' \
    --max-body-size "$DAPR_MAX_BODY_SIZE_MB" \
    --app-port "$ATLAN_APP_HTTP_PORT" \
    --dapr-http-port "$ATLAN_DAPR_HTTP_PORT" \
    --dapr-grpc-port "$ATLAN_DAPR_GRPC_PORT" \
    --metrics-port "$ATLAN_DAPR_METRICS_PORT" \
    --resources-path /app/components \
    &
DAPRD_PID=$!

# Wait for daprd gRPC port to be ready before starting the application.
# We check the gRPC port (not /v1.0/healthz) because daprd binds gRPC before
# it begins waiting for the app on --app-port, so healthz would block until
# the app itself is up — creating a deadlock with our startup sequence.
echo "[entrypoint] Waiting for Dapr sidecar gRPC port..."
RETRIES=0
MAX_RETRIES=60
until python3 -c "import socket; s=socket.create_connection(('localhost', ${ATLAN_DAPR_GRPC_PORT}), timeout=1); s.close()" 2>/dev/null; do
    RETRIES=$((RETRIES + 1))
    if [ "$RETRIES" -ge "$MAX_RETRIES" ]; then
        echo "[entrypoint] ERROR: Dapr sidecar gRPC port not ready after ${MAX_RETRIES} seconds"
        kill -TERM "$DAPRD_PID" 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
echo "[entrypoint] Dapr sidecar is ready"

# Start the application
uv run --no-sync main.py $EXTRA_ARGS &
APP_PID=$!

# Wait for the application to complete
wait $APP_PID
