# ADR-0001: Per-App Handlers vs Uber-Handler

## Status
**Accepted**

## Context

Handlers provide synchronous HTTP endpoints for pre-execution operations:
- **Auth**: Validate credentials before running an app
- **Preflight**: Verify connectivity and permissions
- **Metadata**: Retrieve schema/catalog information for UI configuration

These operations are intentionally outside Temporal (not durable workflows) because they are:
- Short-lived request/response interactions
- Called by external systems (UI, orchestrators) before workflow execution
- Latency-sensitive (users waiting for responses)

We needed to decide how to deploy handlers across multiple apps.

## Decision

We chose **Per-App Handlers**: each app defines and runs its own handler service as a separate container/pod.

## Options Considered

### Option 1: Per-App Handlers (Chosen)

Each app has its own handler service running on its own port/container.

```
RelationalAssets App            Snowflake App
├── Handler Service             ├── Handler Service
│   POST /auth                  │   POST /auth
│   POST /preflight             │   POST /preflight
│   POST /metadata              │   POST /metadata
│   GET /health                 │   GET /health
└── (pipeline-handler:8080)     └── (snowflake-handler:8080)
```

**Pros:**
- **Independent scaling**: Scale handlers based on each app's request volume
- **Isolation**: A crash or memory leak in one handler doesn't affect others
- **Independent deployments**: Update one app's handler without touching others
- **Security boundaries**: Credentials scoped to each handler process
- **Dependency isolation**: Each handler has its own dependencies, avoiding version conflicts
- **Simpler implementation**: Each handler only knows about its own app
- **Clear ownership**: Teams own their handler end-to-end
- **Version coexistence**: Can run v1 and v2 handlers simultaneously during migration

**Cons:**
- **More pods**: Each app adds another container to manage
- **Baseline overhead**: Low-traffic apps still consume resources
- **Configuration duplication**: Similar deployment manifests per app (mitigated by the Helm chart)
- **Service discovery complexity**: Callers need to know which handler endpoint to hit

### Option 2: Uber-Handler (Not Chosen)

A single shared handler service routes requests to all apps.

**Pros:**
- **Fewer pods**: Single container for all handler logic
- **Resource efficiency**: Better utilization across low-traffic apps
- **Single endpoint**: Callers hit one service, routing handled internally

**Cons:**
- **Blast radius**: Bug in one handler affects all apps
- **Coupled deployments**: Any handler change requires full redeployment
- **Dependency conflicts**: All handlers must share compatible dependencies
- **Security risk**: All credentials accessible in one process
- **Scaling inflexibility**: Can't scale individual handlers independently

## Rationale

Handlers are the "front door" for external systems interacting with apps. The per-app pattern provides:

1. **Fault isolation**: A buggy handler (e.g., memory leak parsing large metadata responses) only affects that app.
2. **Independent lifecycle**: Apps evolve at different rates. A stable app's handler shouldn't be redeployed because another app changed.
3. **Security**: Handler processes hold credentials. Limiting scope reduces blast radius of credential exposure.
4. **Operational clarity**: When a handler fails, you know exactly which app is affected.

The uber-handler's efficiency gains don't justify the coupling and risk it introduces.

## Consequences

**Positive:**
- Apps are independently deployable
- Failures are isolated to individual apps
- Clear security boundaries per app

**Negative:**
- Higher baseline resource consumption
- More Kubernetes manifests to maintain (mitigated by the `helm/atlan-app/` chart)

## Implementation

Each app implements the `Handler` ABC:

```python
from application_sdk.handler.base import Handler

class MyHandler(Handler):
    async def test_auth(self, input: AuthInput) -> AuthOutput: ...
    async def preflight_check(self, input: PreflightInput) -> PreflightOutput: ...
    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput: ...
```

`create_app_handler_service()` creates the FastAPI service. The container entrypoint uses `--mode handler`:

```bash
python -m application_sdk.main --mode handler
```
