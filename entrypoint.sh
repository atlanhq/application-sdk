#!/bin/sh
# Entrypoint for Atlan Application SDK containers
set -e

# =============================================================================
# Dual-Process Entrypoint for Atlan Application SDK
#
# Starts daprd (Dapr sidecar) and the application as separate processes.
# Manages lifecycle, health checking, and graceful shutdown of both.
#
# Architecture:
#   entrypoint.sh
#     ├── daprd (sidecar: HTTP :3500, gRPC :50001)
#     └── uv run main.py (application: :8000)
# =============================================================================

DAPRD_PID=""
APP_PID=""

cleanup() {
    echo "[entrypoint] Shutting down..."

    # Stop app first — it may need Dapr during graceful shutdown (e.g., EventStore)
    if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
        echo "[entrypoint] Forwarding SIGTERM to application (PID: $APP_PID)..."
        kill -TERM "$APP_PID" 2>/dev/null || true
        wait "$APP_PID" 2>/dev/null || true
        echo "[entrypoint] Application shutdown complete"
    fi

    # Then stop daprd
    if [ -n "$DAPRD_PID" ] && kill -0 "$DAPRD_PID" 2>/dev/null; then
        echo "[entrypoint] Stopping daprd (PID: $DAPRD_PID)..."
        kill -TERM "$DAPRD_PID" 2>/dev/null || true
        wait "$DAPRD_PID" 2>/dev/null || true
        echo "[entrypoint] daprd shutdown complete"
    fi

    exit 0
}

trap cleanup SIGTERM SIGINT

# Parse DAPR_MAX_BODY_SIZE to integer MB (daprd expects integer, not "1024Mi")
DAPR_MAX_REQUEST_SIZE=$(echo "${DAPR_MAX_BODY_SIZE:-1024Mi}" | sed 's/[^0-9]//g')

# ── Start daprd sidecar ─────────────────────────────────────────────────
echo "[entrypoint] Starting daprd sidecar..."

/usr/bin/daprd \
    --log-level "${DAPR_LOG_LEVEL:-info}" \
    --app-id "${DAPR_APP_ID:-app}" \
    --scheduler-host-address "" \
    --placement-host-address "" \
    --dapr-http-max-request-size "${DAPR_MAX_REQUEST_SIZE}" \
    --app-port "${ATLAN_APP_HTTP_PORT:-8000}" \
    --dapr-http-port "${ATLAN_DAPR_HTTP_PORT:-3500}" \
    --dapr-grpc-port "${ATLAN_DAPR_GRPC_PORT:-50001}" \
    --metrics-port "${ATLAN_DAPR_METRICS_PORT:-3100}" \
    --resources-path /app/components \
    --config /home/appuser/.dapr/config.yaml &

DAPRD_PID=$!

# ── Wait for daprd to be ready ──────────────────────────────────────────
echo "[entrypoint] Waiting for daprd to be ready..."

DAPR_HTTP_PORT="${ATLAN_DAPR_HTTP_PORT:-3500}"
MAX_RETRIES=30
RETRIES=0

while [ "$RETRIES" -lt "$MAX_RETRIES" ]; do
    # Check if daprd is still running
    if ! kill -0 "$DAPRD_PID" 2>/dev/null; then
        echo "[entrypoint] ERROR: daprd exited unexpectedly"
        exit 1
    fi

    # Check daprd health endpoint (using Python since curl/wget may not be available)
    if python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${DAPR_HTTP_PORT}/v1.0/healthz')" 2>/dev/null; then
        echo "[entrypoint] daprd is ready"
        break
    fi

    RETRIES=$((RETRIES + 1))
    sleep 1
done

if [ "$RETRIES" -ge "$MAX_RETRIES" ]; then
    echo "[entrypoint] ERROR: daprd failed to become ready within ${MAX_RETRIES}s"
    kill -TERM "$DAPRD_PID" 2>/dev/null || true
    exit 1
fi

# ── Start the application ───────────────────────────────────────────────
echo "[entrypoint] Starting application..."

uv run --no-sync main.py $EXTRA_ARGS &
APP_PID=$!

# ── Monitor both processes ──────────────────────────────────────────────
# If either process exits, shut down the other.
while true; do
    # Check if app is still running
    if ! kill -0 "$APP_PID" 2>/dev/null; then
        wait "$APP_PID" 2>/dev/null
        APP_EXIT=$?
        echo "[entrypoint] Application exited with code $APP_EXIT"
        if kill -0 "$DAPRD_PID" 2>/dev/null; then
            kill -TERM "$DAPRD_PID" 2>/dev/null || true
            wait "$DAPRD_PID" 2>/dev/null || true
        fi
        exit "$APP_EXIT"
    fi

    # Check if daprd is still running
    if ! kill -0 "$DAPRD_PID" 2>/dev/null; then
        echo "[entrypoint] ERROR: daprd exited unexpectedly"
        if kill -0 "$APP_PID" 2>/dev/null; then
            kill -TERM "$APP_PID" 2>/dev/null || true
            wait "$APP_PID" 2>/dev/null || true
        fi
        exit 1
    fi

    sleep 1
done
