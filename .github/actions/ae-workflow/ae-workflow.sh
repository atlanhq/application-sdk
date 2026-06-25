#!/usr/bin/env bash
# ae-workflow.sh — Register a connector app's AE workflow from its manifest.
#
# App-agnostic and shared across every connector app via the composite action
# in this directory. The DAG is read at runtime from the caller's
# ``app/generated/manifest.json`` (generated from ``contract/app.pkl``) and is
# never hand-copied, so a manifest change can never leave the deploy behind.
# Per-deploy values are filled by a single generic pass — no node-id or per-app
# special-casing:
#   • {app_name}        ← ``name`` in atlan.yaml
#   • {deployment_name} ← --deployment (derives every node's task_queue)
#   • {{token}}         ← the values map (--cred-guid / --connection* /
#                         --extraction-method / --values). A {{token}} with no
#                         value is dropped (e.g. the UI-only {{credential}}).
#   • PublishWorkflow node ← publish/cache flags override existing arg keys
#                            (matched by workflow_type, not node id).
#
# Paths default to the caller's checkout ($GITHUB_WORKSPACE is the cwd for a
# composite action), and can be overridden with --manifest / --atlan-yaml.

set -euo pipefail

AE_URL="http://localhost:8000"
WF_NAME=""
DEPLOYMENT="production"
CRED_GUID=""
CONN_QN="default/app/test"
CONN_NAME="test"
TOKEN=""
EXTRACTION_METHOD="direct"
VALUES_FILE=""
MANIFEST_PATH="app/generated/manifest.json"
ATLAN_YAML="atlan.yaml"
# Publish-side flags. Defaults match production tenants (real Atlas write +
# cache population). CI overrides to "false" so publish stays in dry-run.
CONNECTION_CREATION_ENABLED="false"
EXECUTOR_ENABLED="true"
CONNECTION_CACHE_ENABLED="true"
CONNECTION_CACHE_VIA_APP_ENABLED="true"

while [[ $# -gt 0 ]]; do
  case $1 in
    --ae-url)                    AE_URL="$2";                    shift 2 ;;
    --token)                     TOKEN="$2";                     shift 2 ;;
    --name)                      WF_NAME="$2";                   shift 2 ;;
    --deployment)                DEPLOYMENT="$2";                shift 2 ;;
    --cred-guid)                 CRED_GUID="$2";                 shift 2 ;;
    --connection-qn)             CONN_QN="$2";                   shift 2 ;;
    --connection-name)           CONN_NAME="$2";                 shift 2 ;;
    --extraction-method)         EXTRACTION_METHOD="$2";         shift 2 ;;
    --values)                    VALUES_FILE="$2";               shift 2 ;;
    --manifest)                  MANIFEST_PATH="$2";             shift 2 ;;
    --atlan-yaml)                ATLAN_YAML="$2";                shift 2 ;;
    --connection-creation-enabled) CONNECTION_CREATION_ENABLED="$2"; shift 2 ;;
    --executor-enabled)          EXECUTOR_ENABLED="$2";          shift 2 ;;
    --connection-cache-enabled)  CONNECTION_CACHE_ENABLED="$2";  shift 2 ;;
    --connection-cache-via-app-enabled) CONNECTION_CACHE_VIA_APP_ENABLED="$2"; shift 2 ;;
    # Legacy per-app queue flags — queues are derived from the manifest now,
    # so these are accepted (for callers not yet migrated) but ignored.
    --sigma-queue|--mssql-queue|--mysql-queue|--saphana-queue|--qi-queue|--publish-queue|--lineage-queue) shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# Auth header injected into every AE call when --token is set or ATLAN_API_KEY is in env.
[ -z "$TOKEN" ] && TOKEN="${ATLAN_API_KEY:-}"
CURL_AUTH=()
[ -n "$TOKEN" ] && CURL_AUTH=(-H "Authorization: Bearer $TOKEN")

[ -z "$WF_NAME" ] && WF_NAME="Workflow"
[ -n "$CRED_GUID" ] || { echo "ERROR: --cred-guid is required" >&2; exit 1; }
[ -f "$MANIFEST_PATH" ] || { echo "ERROR: manifest not found at $MANIFEST_PATH" >&2; exit 1; }

err() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "$*" >&2; }

# Reuse-or-create: same-name PUBLISHED workflow → push a new version onto
# its slug; otherwise create a new workflow.
EXISTING_SLUG=$(curl -s "${CURL_AUTH[@]}" "$AE_URL/api/v1/workflows" | python3 -c "
import json, sys
wfs = json.load(sys.stdin).get('data', [])
for w in wfs:
    if w.get('name') == '''$WF_NAME''' and w.get('status') == 'PUBLISHED':
        print(w['slug'])
        break
" 2>/dev/null || true)

if [ -n "$EXISTING_SLUG" ]; then
  log "Found existing published workflow: $EXISTING_SLUG — pushing a new version"
  SLUG="$EXISTING_SLUG"
else
  log "Creating workflow: $WF_NAME"
  CREATE_RESP=$(curl -s -X POST "$AE_URL/api/v1/workflows" \
    "${CURL_AUTH[@]}" \
    -H "Content-Type: application/json" \
    -d "{\"name\": \"$WF_NAME\", \"description\": \"Registered from $MANIFEST_PATH\"}")

  SLUG=$(echo "$CREATE_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('data', {}).get('slug') or d.get('slug')
if not s:
    raise ValueError('No slug in response: ' + json.dumps(d))
print(s)
") || err "Failed to create workflow: $CREATE_RESP"

  log "Workflow slug: $SLUG"
  # AE indexing delay before subsequent calls on production.
  sleep 3
fi

# App name fills the manifest's {app_name} token.
APP_NAME=$(awk '/^name:/{print $2; exit}' "$ATLAN_YAML" 2>/dev/null | tr -d '[:space:]')
APP_NAME="${APP_NAME:-app}"

# Build the version payload from the manifest. A single generic pass fills
# tokens and derives node fields — no per-app or per-node-id logic.
VERSION=$(date +%s)
DAG_PAYLOAD_FILE="$(mktemp)"
trap 'rm -f "$DAG_PAYLOAD_FILE"' EXIT

MANIFEST_PATH="$MANIFEST_PATH" OUT_PATH="$DAG_PAYLOAD_FILE" VERSION="$VERSION" \
APP_NAME="$APP_NAME" DEPLOYMENT="$DEPLOYMENT" CRED_GUID="$CRED_GUID" \
CONN_QN="$CONN_QN" CONN_NAME="$CONN_NAME" EXTRACTION_METHOD="$EXTRACTION_METHOD" \
VALUES_FILE="$VALUES_FILE" \
CONNECTION_CREATION_ENABLED="$CONNECTION_CREATION_ENABLED" \
EXECUTOR_ENABLED="$EXECUTOR_ENABLED" \
CONNECTION_CACHE_ENABLED="$CONNECTION_CACHE_ENABLED" \
CONNECTION_CACHE_VIA_APP_ENABLED="$CONNECTION_CACHE_VIA_APP_ENABLED" \
python3 <<'PYEOF' || err "Failed to build DAG payload from manifest"
import json, os

_DROP = object()  # sentinel: a {{token}} with no value drops its key

def as_bool(v):
    return str(v).strip().lower() == "true"

manifest = json.load(open(os.environ["MANIFEST_PATH"]))
dag = manifest["dag"]
app = os.environ["APP_NAME"]
deployment = os.environ["DEPLOYMENT"]

# {{token}} → value map. A token absent here is dropped from the payload
# (treated as "not provided" — e.g. the UI-only {{credential}} / {{preflight-check}}
# fields the runtime doesn't need). Extend or override via --values.
values = {
    "credential-guid": os.environ["CRED_GUID"],
    "connection": {
        "connection_name": os.environ["CONN_NAME"],
        "connection_qualified_name": os.environ["CONN_QN"],
    },
    "extraction-method": os.environ["EXTRACTION_METHOD"],
    "include-filter": {},
    "exclude-filter": {},
}
values_file = os.environ.get("VALUES_FILE") or ""
if values_file:
    values.update(json.load(open(values_file)))

# Publish-side flags aren't manifest tokens yet, so override the matching keys
# on the PublishWorkflow node (matched by workflow_type, not node id).
publish_overrides = {
    "connection_creation_enabled": as_bool(os.environ["CONNECTION_CREATION_ENABLED"]),
    "executor_enabled": as_bool(os.environ["EXECUTOR_ENABLED"]),
    "connection_cache_enabled": as_bool(os.environ["CONNECTION_CACHE_ENABLED"]),
    "connection_cache_via_app_enabled": as_bool(os.environ["CONNECTION_CACHE_VIA_APP_ENABLED"]),
}

def sub(obj):
    """Recursively fill manifest tokens. A string that is exactly ``{{token}}``
    becomes its (possibly non-string) value or _DROP; ``{app_name}`` /
    ``{deployment_name}`` are substring-replaced; everything else is unchanged."""
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            filled = sub(val)
            if filled is not _DROP:
                out[key] = filled
        return out
    if isinstance(obj, list):
        return [x for x in (sub(i) for i in obj) if x is not _DROP]
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{{") and s.endswith("}}") and s.count("{{") == 1:
            return values.get(s[2:-2], _DROP)
        return obj.replace("{app_name}", app).replace("{deployment_name}", deployment)
    return obj

new_dag = {}
for node_id, node in dag.items():
    node = sub(node)
    inp = node.setdefault("inputs", {})
    # Child-workflow nodes carry node_type + app_task_queue, which the manifest
    # omits; derive both from the (now-resolved) node.
    if node.get("activity_name") == "execute_workflow":
        node["node_type"] = "workflow"
        queue = inp.get("task_queue")
        if queue:
            node["app_task_queue"] = queue
    # Apply publish flags to the PublishWorkflow node, overriding only keys that
    # already exist (never adding app-specific ones).
    if inp.get("workflow_type") == "PublishWorkflow":
        args = inp.setdefault("args", {})
        for key, val in publish_overrides.items():
            if key in args:
                args[key] = val
    new_dag[node_id] = node

json.dump({"version": int(os.environ["VERSION"]), "dag": new_dag}, open(os.environ["OUT_PATH"], "w"))
PYEOF

for _attempt in 1 2 3; do
  VERSION_RESP=$(curl -s -X POST "$AE_URL/api/v1/workflows/$SLUG/versions" \
    "${CURL_AUTH[@]}" \
    -H "Content-Type: application/json" \
    --data-binary @"$DAG_PAYLOAD_FILE")
  REAL_VERSION=$(echo "$VERSION_RESP" | python3 -c "
import json, sys
d = json.load(sys.stdin)
v = d.get('data', {}).get('version') or d.get('version')
if v:
    print(v)
" 2>/dev/null || true)
  [ -n "$REAL_VERSION" ] && break
  log "Version create attempt $_attempt failed (workflow not ready yet), retrying in 5s..."
  sleep 5
done
[ -n "$REAL_VERSION" ] || err "Failed to create version after 3 attempts: $VERSION_RESP"

log "Version created: $REAL_VERSION"

# Delete auto-created empty version, if any.
ALL_VERSIONS=$(curl -s "${CURL_AUTH[@]}" "$AE_URL/api/v1/workflows/$SLUG/versions")
EMPTY_VERSION=$(echo "$ALL_VERSIONS" | python3 -c "
import json, sys
vs = json.load(sys.stdin).get('data', [])
for v in vs:
    dag = v.get('dag') or {}
    if not dag or dag == {}:
        print(v['version'])
        break
" 2>/dev/null || true)

if [ -n "$EMPTY_VERSION" ] && [ "$EMPTY_VERSION" != "$REAL_VERSION" ]; then
  log "Deleting auto-created empty version: $EMPTY_VERSION"
  curl -s -X DELETE "${CURL_AUTH[@]}" "$AE_URL/api/v1/workflows/$SLUG/versions/$EMPTY_VERSION" >/dev/null
fi

# Publish the version.
log "Publishing version $REAL_VERSION..."
PUB_RESP=$(curl -s -X POST "${CURL_AUTH[@]}" "$AE_URL/api/v1/workflows/$SLUG/versions/$REAL_VERSION/publish")
PUB_STATUS=$(echo "$PUB_RESP" | python3 -c "
import json, sys
print(json.load(sys.stdin).get('status', 'unknown'))
" 2>/dev/null || echo "unknown")

[ "$PUB_STATUS" = "success" ] || err "Publish failed: $PUB_RESP"
log "Published. Workflow ready."

# Emit slug to stdout (last line) and, if running inside a composite action,
# to the action output.
echo "$SLUG"
[ -n "${GITHUB_OUTPUT:-}" ] && echo "slug=$SLUG" >> "$GITHUB_OUTPUT" || true
