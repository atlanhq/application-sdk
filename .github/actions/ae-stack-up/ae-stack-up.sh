#!/usr/bin/env bash
# ae-stack-up.sh — Boot the local Automation Engine stack for a connector's
# full-DAG test, deciding which sibling apps to start *from the manifest*.
#
# The connector never hand-lists "I need publish + qi + lineage". Instead this
# reads app/generated/manifest.json -> dag, maps each node's task_queue to the
# owning app, and starts exactly those workers (always AE + the connector;
# publish / query-intelligence / lineage only when the DAG references them).
#
# Everything runs in-runner: Temporal dev server + Dapr (slim) + one `dapr run`
# per app. No live tenant is contacted for orchestration — only the source
# (via injected creds) and, for the publish app, OAuth to fetch a token.
#
# Inputs come from env (set by action.yaml):
#   WORKSPACE         — $GITHUB_WORKSPACE (parent of all checkouts)
#   APP_NAME          — connector short name (e.g. monte-carlo); also atlan.yaml name
#   APP_DIR           — connector checkout dir (default: $WORKSPACE/<repo>)
#   DEPLOYMENT        — deployment tier; fills {deployment_name} (default: ci)
#   MANIFEST_PATH     — manifest, relative to APP_DIR (default app/generated/manifest.json)
#   ORG_PAT_GITHUB    — PAT to clone the private sibling app repos
#   ATLAN_BASE_URL / ATLAN_CLIENT_ID / ATLAN_CLIENT_SECRET — publish OAuth (only
#                       needed when the DAG has a publish node)
#
# Emits to $GITHUB_OUTPUT (when run under Actions):
#   ae-url=http://localhost:8000
#   started-apps=<space-separated app ids>
set -euo pipefail

WORKSPACE="${WORKSPACE:-$PWD}"
DEPLOYMENT="${DEPLOYMENT:-ci}"
MANIFEST_PATH="${MANIFEST_PATH:-app/generated/manifest.json}"
APP_DIR="${APP_DIR:-$WORKSPACE/$APP_NAME}"
LOG_DIR="$WORKSPACE/logs"; mkdir -p "$LOG_DIR"

log() { echo "[ae-stack-up] $*"; }
err() { echo "::error::[ae-stack-up] $*" >&2; exit 1; }

MANIFEST="$APP_DIR/$MANIFEST_PATH"
[ -f "$MANIFEST" ] || err "manifest not found at $MANIFEST"

# ── 1. Decide which apps the DAG needs (manifest-driven) ─────────────────────
# Maps each dag node's task_queue prefix to the owning sibling app. The
# connector + AE are implicit. Output: newline list of "app_key|repo|queue".
NEEDS=$(APP_NAME="$APP_NAME" DEPLOYMENT="$DEPLOYMENT" python3 - "$MANIFEST" <<'PYEOF'
import json, os, sys
m = json.load(open(sys.argv[1]))
dep = os.environ["DEPLOYMENT"]
dag = m.get("dag", {})
# queue-prefix -> (app_key, repo). The connector itself is handled separately.
KNOWN = {
    "atlan-publish":             ("publish",           "atlan-publish-app"),
    "atlan-query-intelligence":  ("query-intelligence","atlan-query-intelligence-app"),
    "atlan-lineage":             ("lineage",           "atlan-lineage-app"),
}
seen = {}
for node in dag.values():
    q = (node.get("inputs", {}) or {}).get("task_queue", "") or ""
    q = q.replace("{deployment_name}", dep)
    for prefix, (key, repo) in KNOWN.items():
        if q.startswith(prefix + "-") and key not in seen:
            seen[key] = (repo, q)
for key, (repo, q) in seen.items():
    print(f"{key}|{repo}|{q}")
PYEOF
)
log "DAG needs sibling apps: $(echo "$NEEDS" | cut -d'|' -f1 | tr '\n' ' ' )"

# ── 2. Toolchain: Dapr (slim) + Temporal CLI + uv ────────────────────────────
DAPR_VERSION="1.16.2"
if ! command -v dapr >/dev/null 2>&1; then
  wget -q "https://github.com/dapr/cli/releases/download/v${DAPR_VERSION}/dapr_linux_amd64.tar.gz" -O /tmp/dapr.tar.gz
  tar -xzf /tmp/dapr.tar.gz -C /tmp && sudo mv /tmp/dapr /usr/local/bin/
  dapr init --runtime-version "${DAPR_VERSION}" --slim
fi
command -v temporal >/dev/null 2>&1 || curl -sSf https://temporal.download/cli.sh | sh
export PATH="$HOME/.dapr/bin:$HOME/.temporalio/bin:$PATH"

# Shared object-store bucket so every app's localstorage binding resolves the
# same files on disk (extract writes, publish/qi/lineage read).
SHARED_ROOT="$WORKSPACE/_ae-bucket/atlan-bucket"
mkdir -p "$SHARED_ROOT"

write_components() {
  # $1 = app checkout dir
  mkdir -p "$1/components" "$1/local/dapr"   # local/dapr must exist for the sqlite statestore
  cat > "$1/components/objectstore.yaml" <<EOF
apiVersion: dapr.io/v1alpha1
kind: Component
metadata: { name: objectstore }
spec:
  version: v1
  type: bindings.localstorage
  ignoreErrors: true
  metadata:
    - name: rootPath
      value: "${SHARED_ROOT}"
EOF
  cat > "$1/components/statestore.yaml" <<'EOF'
apiVersion: dapr.io/v1alpha1
kind: Component
metadata: { name: statestore }
spec:
  version: v1
  type: state.sqlite
  ignoreErrors: true
  metadata:
    - name: connectionString
      value: "./local/dapr/statestore.db"
EOF
}

clone_app() {
  # $1 = repo name under atlanhq
  local repo="$1"
  [ -d "$WORKSPACE/$repo" ] && return 0
  git clone --depth 1 \
    "https://x-access-token:${ORG_PAT_GITHUB}@github.com/atlanhq/${repo}.git" \
    "$WORKSPACE/$repo" >/dev/null 2>&1 || err "clone failed: $repo"
}

# Always need the Automation Engine.
clone_app atlan-automation-engine-app
AE_DIR="$WORKSPACE/atlan-automation-engine-app"

# uv
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# ── 3. Start Temporal dev server ─────────────────────────────────────────────
log "starting Temporal dev server"
temporal server start-dev --db-filename /tmp/temporal.db > "$LOG_DIR/temporal.log" 2>&1 &
for i in $(seq 1 30); do
  temporal workflow list >/dev/null 2>&1 && { log "Temporal ready (${i}s)"; break; }
  [ "$i" -eq 30 ] && err "Temporal failed to start"
  sleep 1
done

# ── 4. Generic worker launcher ───────────────────────────────────────────────
# Every app starts the same way: uv sync, dapr-run its main.py with a unique
# port block + its task queue. Ports are derived from a per-app index so they
# never collide.
PORT_IDX=0
STARTED=""
start_worker() {
  # $1 dir  $2 app-id  $3 ATLAN_APPLICATION_NAME  $4 task_queue  $5 extra_env(optional)
  local dir="$1" appid="$2" appname="$3" queue="$4" extra="${5:-}"
  local app_port=$((3001 + PORT_IDX * 10))
  local dhttp=$((3511 + PORT_IDX * 10))
  local dgrpc=$((50012 + PORT_IDX * 10))
  local dinternal=$((50013 + PORT_IDX * 10))
  local metrics=$((3112 + PORT_IDX * 10))
  local prom=$((9466 + PORT_IDX))
  write_components "$dir"
  ( cd "$dir" && uv sync --all-groups >/dev/null 2>&1 || uv sync >/dev/null 2>&1 || true )
  log "starting $appid (queue=$queue, port=$app_port)"
  # $extra is placed LAST so a worker can override a default (e.g. the connector
  # sets ATLAN_DEPLOYMENT_NAME=local to unlock the dev local-vault while still
  # listening on the explicit ATLAN_TASK_QUEUE below).
  ( cd "$dir" && env \
      ATLAN_APPLICATION_NAME="$appname" \
      ATLAN_DEPLOYMENT_NAME="$DEPLOYMENT" \
      ATLAN_WORKFLOW_HOST=localhost ATLAN_WORKFLOW_PORT=7233 \
      ATLAN_TASK_QUEUE="$queue" \
      ATLAN_APP_HTTP_PORT="$app_port" ATLAN_HANDLER_PORT="$app_port" \
      ATLAN_DAPR_HTTP_PORT="$dhttp" ATLAN_DAPR_GRPC_PORT="$dgrpc" \
      ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS="0.0.0.0:${prom}" \
      $extra \
      dapr run --app-id "$appid" --app-port "$app_port" \
        --dapr-http-port "$dhttp" --dapr-grpc-port "$dgrpc" \
        --dapr-internal-grpc-port "$dinternal" --metrics-port "$metrics" \
        --scheduler-host-address '' --placement-host-address '' \
        --max-body-size 100Mi --resources-path components --log-level warn \
        -- uv run python main.py > "$LOG_DIR/${appid}.log" 2>&1 & )
  STARTED="$STARTED $appid"
  PORT_IDX=$((PORT_IDX + 1))
}

wait_ready() { # $1 app-id  $2 grep-marker  $3 timeout
  local appid="$1" marker="$2" to="${3:-120}"
  for i in $(seq 1 "$to"); do
    grep -q "$marker" "$LOG_DIR/${appid}.log" 2>/dev/null && { log "$appid ready (${i}s)"; return 0; }
    [ "$i" -eq "$to" ] && { tail -30 "$LOG_DIR/${appid}.log" 2>/dev/null; err "$appid not ready in ${to}s"; }
    sleep 1
  done
}

# AE first (port idx 0 = http 8000-ish; AE serves the workflow API).
# AE is special: it exposes the workflow API the reusable POSTs to, on :8000.
write_components "$AE_DIR"
( cd "$AE_DIR" && uv sync --all-groups >/dev/null 2>&1 || true )
log "starting automation-engine on :8000"
( cd "$AE_DIR" && env \
    ATLAN_APPLICATION_NAME=automation-engine ATLAN_DEPLOYMENT_NAME="$DEPLOYMENT" \
    ATLAN_REGISTRY_TYPE=sql ATLAN_WORKFLOW_HOST=localhost ATLAN_WORKFLOW_PORT=7233 \
    ATLAN_APP_HTTP_PORT=8000 ATLAN_HANDLER_PORT=8000 \
    ATLAN_DAPR_HTTP_PORT=3500 ATLAN_DAPR_GRPC_PORT=50001 \
    ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS=0.0.0.0:9465 \
    dapr run --app-id automation-engine --app-port 8000 \
      --dapr-http-port 3500 --dapr-grpc-port 50001 --dapr-internal-grpc-port 50002 \
      --metrics-port 3100 --scheduler-host-address '' --placement-host-address '' \
      --resources-path components --log-level warn \
      -- uv run automation_engine/main.py > "$LOG_DIR/automation-engine.log" 2>&1 & )
for i in $(seq 1 60); do
  curl -sf "http://localhost:8000/api/v1/workflows" >/dev/null 2>&1 && { log "AE ready (${i}s)"; break; }
  [ "$i" -eq 60 ] && { tail -30 "$LOG_DIR/automation-engine.log"; err "AE failed to start"; }
  sleep 2
done

# The connector worker (always; serves the extract node + the dev local-vault).
# ATLAN_DEPLOYMENT_NAME=local unlocks /workflows/v1/dev/local-vault (else 403);
# the queue stays atlan-<app>-<deployment> via the explicit ATLAN_TASK_QUEUE.
# ATLAN_APP_MODULE/ATLAN_HANDLER_MODULE MUST be set or the worker comes up with
# DefaultHandler (SDR workflows only) and the AE-dispatched extraction workflow
# never gets picked up — the child workflow then runs forever.
CONN_EXTRA="ATLAN_LOCAL_DEVELOPMENT=true ATLAN_HANDLER_HOST=0.0.0.0 ATLAN_DEPLOYMENT_NAME=local"
[ -n "${APP_MODULE:-}" ]     && CONN_EXTRA="$CONN_EXTRA ATLAN_APP_MODULE=${APP_MODULE}"
[ -n "${HANDLER_MODULE:-}" ] && CONN_EXTRA="$CONN_EXTRA ATLAN_HANDLER_MODULE=${HANDLER_MODULE}"
start_worker "$APP_DIR" "${APP_NAME}-app" "$APP_NAME" "atlan-${APP_NAME}-${DEPLOYMENT}" "$CONN_EXTRA"
wait_ready "${APP_NAME}-app" "atlan-${APP_NAME}-${DEPLOYMENT}" 90

# Publish OAuth .env bits are passed via extra-env when publish is needed.
PUBLISH_ENV=""
if [ -n "${ATLAN_BASE_URL:-}" ]; then
  TOKEN_URL=$(curl -sf "${ATLAN_BASE_URL}/auth/realms/default/.well-known/openid-configuration" \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['token_endpoint'])" 2>/dev/null \
    || echo "${ATLAN_BASE_URL}/auth/realms/default/protocol/openid-connect/token")
  PUBLISH_ENV="ATLAN_OAUTH2_CLIENT_ID=${ATLAN_CLIENT_ID:-} ATLAN_OAUTH2_CLIENT_SECRET=${ATLAN_CLIENT_SECRET:-} ATLAN_CLIENT_ID=${ATLAN_CLIENT_ID:-} ATLAN_CLIENT_SECRET=${ATLAN_CLIENT_SECRET:-} ATLAN_BASE_URL=${ATLAN_BASE_URL} ATLAN_TOKEN_URL=${TOKEN_URL}"
fi

# Now the manifest-selected siblings.
while IFS='|' read -r key repo queue; do
  [ -z "$key" ] && continue
  clone_app "$repo"
  case "$key" in
    publish)            start_worker "$WORKSPACE/$repo" publish-app          publish            "$queue" "$PUBLISH_ENV"
                        wait_ready publish-app "task queue: $queue" 180 ;;
    query-intelligence) start_worker "$WORKSPACE/$repo" query-intelligence   query-intelligence "$queue"
                        wait_ready query-intelligence "task queue: $queue" 180 ;;
    lineage)            start_worker "$WORKSPACE/$repo" lineage-app          lineage            "$queue"
                        wait_ready lineage-app "task queue: $queue" 180 ;;
  esac
done <<< "$NEEDS"

log "stack up. started:$STARTED automation-engine"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "ae-url=http://localhost:8000" >> "$GITHUB_OUTPUT"
  echo "started-apps=automation-engine ${APP_NAME}-app${STARTED}" >> "$GITHUB_OUTPUT"
fi
