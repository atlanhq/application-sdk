# ADR-0002: Per-App Workers vs Uber-Worker

## Status
**Accepted**

## Context

Workers are Temporal processes that execute app workflows and their activities. When an app is triggered, a worker picks up the task from a queue and runs the workflow to completion.

We needed to decide how to deploy workers across multiple apps:
- Should each app have its own worker with a dedicated task queue?
- Or should one shared worker run all apps on a single task queue?

## Decision

We chose **Per-App Workers**: each app runs on its own worker with a dedicated task queue, even for related apps (e.g., parent-child workflow relationships).

## Options Considered

### Option 1: Per-App Workers (Chosen)

Each app has its own worker process and task queue.

```
Relational Assets Worker          Snowflake Extractor Worker
├── Task Queue: relational-queue  ├── Task Queue: snowflake-queue
└── RelationalAssetsPipeline          └── SnowflakeExtractor
        │                                  │
        │  call_by_name("loader")           │  call_by_name("loader")
        ▼                                  ▼
                    Loader Worker
                    ├── Task Queue: loader-queue
                    └── Loader app
```

Both callers invoke the shared Loader app, but each runs in its own worker. Upgrading one app's worker doesn't require restarting the other.

**Pros:**
- **Scale to zero**: KEDA scales workers to 0 replicas when task queues are empty
- **Independent scaling**: KEDA scales each worker based on its own task queue depth
- **Isolation**: A stuck or crashed worker only affects its own app
- **Resource tuning**: Allocate different CPU/memory per app based on workload
- **Independent deployments**: Deploy/upgrade workers without affecting other apps
- **Dependency isolation**: Each worker has its own Python environment
- **Decoupled lifecycles**: Even related apps (parent calling child) evolve independently
- **Version coexistence**: Run v1 and v2 workers simultaneously during migration

**Cons:**
- **More processes**: Each app requires a separate worker deployment
- **Configuration duplication**: Similar worker configurations per app (mitigated by the Helm chart)

### Option 2: Uber-Worker (Not Chosen)

A single shared worker runs all app workflows on one task queue.

**Pros:**
- **Fewer processes**: Single worker deployment
- **Simpler configuration**: One worker config to maintain

**Cons:**
- **No scale to zero**: Must keep worker running even when all apps are idle
- **Blast radius**: A bug or OOM in one app's activity crashes the worker for all apps
- **Coupled deployments**: Any app change requires redeploying the shared worker
- **Scaling inflexibility**: Can't scale individual apps based on their queue depth
- **Dependency conflicts**: All apps must share compatible Python dependencies
- **Resource contention**: CPU/memory-heavy apps starve others

## Rationale

1. **Cost efficiency**: KEDA scales workers to zero when idle. Apps that run infrequently consume no resources between runs.
2. **Fault isolation**: A memory leak or infinite loop in one app's activity doesn't take down workers for other apps.
3. **Precise scaling**: KEDA scales each worker based on its specific task queue depth.
4. **Independent lifecycle**: Even related apps evolve independently. Upgrading a parent app doesn't require restarting child app workers.
5. **Temporal best practice**: Temporal recommends dedicated task queues per workflow type for production deployments.

## Consequences

**Positive:**
- Apps are independently scalable and deployable
- Idle apps consume zero resources (scale to zero)
- Failures are isolated
- KEDA can optimize resource usage per app

**Negative:**
- More Kubernetes deployments to manage (mitigated by the Helm chart)
- Parent-child workflow calls cross task queue boundaries (Temporal handles this transparently)

## Implementation

```bash
python -m application_sdk.main --mode worker
```

`create_worker()` auto-discovers all registered apps and tasks from `AppRegistry`/`TaskRegistry`. No manual `Worker(workflow_classes=[...], activities=[...])` needed.

The Helm chart generates a KEDA `ScaledObject` per worker targeting its task queue depth, with `minReplicaCount: 0` for scale-to-zero.
