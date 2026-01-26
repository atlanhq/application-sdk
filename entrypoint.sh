#!/bin/sh
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

    if [ -z "$APP_PID" ]; then
        # Fallback: find python process directly
        APP_PID=$(ps aux | grep "[p]ython.*main.py" | awk '{print $1}' | head -1)
    fi

    if [ -n "$APP_PID" ]; then
        echo "[entrypoint] Forwarding SIGTERM to application (PID: $APP_PID)..."
        kill -TERM "$APP_PID" 2>/dev/null || true

        # Wait for graceful shutdown to complete
        tail --pid="$APP_PID" -f /dev/null 2>/dev/null || true
        echo "[entrypoint] Application shutdown complete"
    else
        echo "[entrypoint] Warning: Could not find application process"
        # Wait for the main command to exit on its own
        wait $CMD_PID 2>/dev/null || true
    fi

    exit 0
}

trap term_handler SIGTERM SIGINT

# Run the provided command in background
"$@" &
CMD_PID=$!

# Wait for the command
wait $CMD_PID
