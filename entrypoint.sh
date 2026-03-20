#!/bin/sh
# Entrypoint script for Application SDK containers.
#
# Starts daprd (DAPR runtime) directly alongside the Python application,
# giving full control over graceful shutdown behaviour.
#
# Why daprd directly instead of `dapr run`:
#   `dapr run` is a CLI convenience wrapper that starts daprd internally but
#   does NOT expose --dapr-graceful-shutdown-seconds. We need this flag so
#   daprd waits long enough for in-flight Python (Temporal) activities to
#   complete when KEDA scales the pod to zero. Calling daprd directly exposes
#   all daprd flags.
#
# Environment variables (all optional, with sensible defaults):
#   DAPR_APP_ID          - DAPR application ID (default: $ATLAN_SERVICE_NAME or "app")
#   DAPR_APP_PORT        - Application port DAPR connects to (default: 8080)
#   DAPR_HTTP_PORT       - DAPR HTTP API port (default: 3500)
#   DAPR_GRPC_PORT       - DAPR gRPC API port (default: 50001)
#   DAPR_COMPONENTS_PATH - Path to DAPR component YAML files (default: /app/components)
#   DAPR_LOG_LEVEL                  - DAPR log level (default: warn)
#   DAPR_METRICS_PORT               - Port for daprd Prometheus metrics (default: 3100)
#   DAPR_MAX_BODY_SIZE              - Max request body size passed to daprd as
#                                     --max-request-body-size (default: 1024Mi)
#   DAPR_SCHEDULER_HOST_ADDRESS     - DAPR scheduler address (default: empty = disabled)
#   DAPR_GRACEFUL_SHUTDOWN_SECONDS  - How long daprd waits for in-flight requests after
#                                     receiving SIGTERM (default: 3600). Must be >=
#                                     APP_GRACEFUL_SHUTDOWN_TIMEOUT.

set -eu

# ---------------------------------------------------------------------------
# Configuration with defaults — exported so Python child process can read them
# ---------------------------------------------------------------------------
export DAPR_APP_ID="${DAPR_APP_ID:-${ATLAN_SERVICE_NAME:-app}}"
export DAPR_APP_PORT="${DAPR_APP_PORT:-8080}"
export DAPR_HTTP_PORT="${DAPR_HTTP_PORT:-3500}"
export DAPR_GRPC_PORT="${DAPR_GRPC_PORT:-50001}"
export DAPR_COMPONENTS_PATH="${DAPR_COMPONENTS_PATH:-/app/components}"
export DAPR_LOG_LEVEL="${DAPR_LOG_LEVEL:-warn}"
export DAPR_METRICS_PORT="${DAPR_METRICS_PORT:-3100}"
export DAPR_MAX_BODY_SIZE="${DAPR_MAX_BODY_SIZE:-1024Mi}"
export DAPR_SCHEDULER_HOST_ADDRESS="${DAPR_SCHEDULER_HOST_ADDRESS:-}"
# How long daprd waits for in-flight requests to complete after receiving SIGTERM.
# Must be >= APP_GRACEFUL_SHUTDOWN_TIMEOUT and < terminationGracePeriodSeconds
# so Kubernetes always gets the last word.
export DAPR_GRACEFUL_SHUTDOWN_SECONDS="${DAPR_GRACEFUL_SHUTDOWN_SECONDS:-3600}"

# PIDs managed by this script
DAPRD_PID=""
APP_PID=""

# ---------------------------------------------------------------------------
# Signal handling — forward SIGTERM to Python first, stop daprd after it exits
# ---------------------------------------------------------------------------
forward_signal() {
    if [ -n "${APP_PID}" ]; then
        echo "[entrypoint] Forwarding SIGTERM to Python app PID ${APP_PID}"
        kill -TERM "${APP_PID}" 2>/dev/null || true
        wait "${APP_PID}" 2>/dev/null || true
    fi
    if [ -n "${DAPRD_PID}" ]; then
        echo "[entrypoint] Python exited, stopping daprd PID ${DAPRD_PID}"
        kill -TERM "${DAPRD_PID}" 2>/dev/null || true
        wait "${DAPRD_PID}" 2>/dev/null || true
    fi
    exit 0
}

trap forward_signal SIGTERM SIGINT

# ---------------------------------------------------------------------------
# Start daprd directly (not via `dapr run`)
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting daprd"
echo "[entrypoint]   app-id:          ${DAPR_APP_ID}"
echo "[entrypoint]   app-port:        ${DAPR_APP_PORT}"
echo "[entrypoint]   dapr-http-port:  ${DAPR_HTTP_PORT}"
echo "[entrypoint]   dapr-grpc-port:  ${DAPR_GRPC_PORT}"
echo "[entrypoint]   components-path: ${DAPR_COMPONENTS_PATH}"
echo "[entrypoint]   log-level:       ${DAPR_LOG_LEVEL}"
echo "[entrypoint]   metrics-port:    ${DAPR_METRICS_PORT}"
echo "[entrypoint]   max-body-size:   ${DAPR_MAX_BODY_SIZE}"
echo "[entrypoint]   graceful-shutdown-seconds: ${DAPR_GRACEFUL_SHUTDOWN_SECONDS}"

daprd \
    --app-id "${DAPR_APP_ID}" \
    --app-port "${DAPR_APP_PORT}" \
    --dapr-http-port "${DAPR_HTTP_PORT}" \
    --dapr-grpc-port "${DAPR_GRPC_PORT}" \
    --resources-path "${DAPR_COMPONENTS_PATH}" \
    --log-level "${DAPR_LOG_LEVEL}" \
    --metrics-port "${DAPR_METRICS_PORT}" \
    --max-body-size "${DAPR_MAX_BODY_SIZE}" \
    --placement-host-address "" \
    --scheduler-host-address "${DAPR_SCHEDULER_HOST_ADDRESS}" \
    --dapr-graceful-shutdown-seconds "${DAPR_GRACEFUL_SHUTDOWN_SECONDS}" &
DAPRD_PID=$!
echo "[entrypoint] daprd started with PID ${DAPRD_PID}"

# Daprd 1.14+ delays /v1.0/healthz until ALL components finish initializing.
# Slow components (S3 binding, Kubernetes secretstore) can take > 30s, which
# exceeds the pod liveness probe window — Python never starts — deadlock.
#
# Solution: start Python immediately after a tiny sleep (daprd's HTTP server
# binds in < 1s). This matches the original `dapr run` behaviour where both
# processes started simultaneously. The Python app handles transient DAPR
# errors gracefully via retries.
#
# We still abort early if daprd crashes at startup (exit code check below).
sleep 0.5
if ! kill -0 "${DAPRD_PID}" 2>/dev/null; then
    echo "[entrypoint] ERROR: daprd exited unexpectedly during startup"
    exit 1
fi
echo "[entrypoint] daprd started (component init may still be in progress)"

# ---------------------------------------------------------------------------
# Launch the Python application
# ---------------------------------------------------------------------------
echo "[entrypoint] Starting Python app"
uv run --no-sync python -m application_sdk.main "$@" &
APP_PID=$!
echo "[entrypoint] App started with PID ${APP_PID}"

# Wait for the app to exit
wait "${APP_PID}"
EXIT_CODE=$?

echo "[entrypoint] App exited with code ${EXIT_CODE}"

# Stop daprd after the app exits (normal path — signal handler covers SIGTERM path)
if kill -0 "${DAPRD_PID}" 2>/dev/null; then
    echo "[entrypoint] Stopping daprd"
    kill -TERM "${DAPRD_PID}" 2>/dev/null || true
    wait "${DAPRD_PID}" 2>/dev/null || true
fi

exit ${EXIT_CODE}
