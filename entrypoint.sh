#!/bin/sh
# Graceful shutdown entrypoint for Atlan Application SDK containers
set -e

# =============================================================================
# Graceful Shutdown Wrapper for Atlan Application SDK
#
# This script wraps any command and ensures SIGTERM is forwarded to the
# Python/uv process for graceful shutdown of Temporal activities.
#
# Usage: entrypoint.sh <command> [args...]
#
# Examples:
#   entrypoint.sh dapr run ... -- uv run main.py
#   entrypoint.sh sh -c "dapr run ... -- uv run main.py"
# =============================================================================

term_handler() {
    echo "[entrypoint] Received SIGTERM, initiating graceful shutdown..."

    # Find uv process first, fallback to python
    APP_PID=$(ps aux | grep "[u]v run" | awk '{print $1}' | head -1)

    echo "[entrypoint] Forwarding SIGTERM to application (PID: $APP_PID)..."
    kill -TERM "$APP_PID" 2>/dev/null || true

    # Wait for graceful shutdown to complete
    tail --pid="$APP_PID" -f /dev/null 2>/dev/null || true
    echo "[entrypoint] Application shutdown complete"

    exit 0
}

trap term_handler SIGTERM SIGINT

# Run the provided command in background
dapr run  \
    --log-level $DAPR_LOG_LEVEL \
    --app-id $DAPR_APP_ID \
    --scheduler-host-address '' \
    --placement-host-address '' \
    --max-body-size $DAPR_MAX_BODY_SIZE \
    --app-port $ATLAN_APP_HTTP_PORT \
    --dapr-http-port $ATLAN_DAPR_HTTP_PORT \
    --dapr-grpc-port $ATLAN_DAPR_GRPC_PORT \
    --metrics-port $ATLAN_DAPR_METRICS_PORT \
    --resources-path /app/components \
    uv run --no-sync main.py $EXTRA_ARGS &

CMD_PID=$!

# Wait for the command
wait $CMD_PID
