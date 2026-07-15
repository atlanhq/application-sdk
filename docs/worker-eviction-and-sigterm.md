# Worker eviction, SIGTERM, and activity retries

How the SDK handles worker pod termination, and when a termination does or does not consume an activity's `RetryPolicy.max_attempts` budget.

## TL;DR

- On `SIGTERM`/`SIGINT` the SDK sets a process-wide shutdown flag, then lets the Temporal worker drain. When an in-flight activity is cancelled during shutdown, the activity wrapper re-raises the `asyncio.CancelledError` as `ApplicationError(type="WorkerEvicted", non_retryable=True)`.
- `WorkerEvicted` is always added to the activity's `non_retryable_error_types`, so Temporal never retries it. A workflow-side loop (`execute_activity_with_eviction_retry`) re-dispatches the activity as a fresh attempt under its own counter. The eviction therefore does **not** consume the task's `max_attempts` budget.
- This protection only holds when the worker process survives long enough to (1) cancel the activity and (2) report the failure to the server. Abrupt terminations - spot reclaim, OOMKill, node loss - kill the process first. The activity is never reported as `WorkerEvicted`; the server times it out on heartbeat instead, which is retryable and **does** consume `max_attempts`.
- A `SIGTERM` carries no cause. The SDK cannot tell a spot reclaim from a rolling deploy, and does not try. Distinguishing requires out-of-band cloud or Kubernetes signals.

## The shutdown path

```
SIGTERM / SIGINT
  -> signal handler sets the shutdown flag, then runs worker shutdown
        main.py: _install_graceful_signal_handlers -> shutdown.mark_worker_shutting_down()
  -> AppWorker.__aexit__ sleeps SHUTDOWN_DRAIN_DELAY_SECONDS (flush queued completions),
     then delegates to Worker.__aexit__
        worker.py: AppWorker.__aexit__   (see related RCA)
  -> worker stops polling, waits graceful_shutdown_timeout for in-flight activities,
     then CANCELS the survivors
        worker.py: graceful_shutdown_timeout wiring; settings.py: graceful_shutdown_timeout_seconds
  -> cancelled activity body raises asyncio.CancelledError
        activities.py: `except asyncio.CancelledError`
           - shutdown flag set -> raise ApplicationError(type=WorkerEvicted, non_retryable=True)
           - flag not set       -> re-raise CancelledError (ordinary workflow-driven cancel)
  -> WorkerEvicted is non-retryable at the Temporal layer
        retry.py: _with_worker_evicted_non_retryable (always appends WORKER_EVICTED_TYPE)
  -> workflow re-dispatches a FRESH activity, bounded by WORKER_EVICTION_MAX_RETRIES
        eviction_retry.py: execute_activity_with_eviction_retry / _is_worker_evicted
```

Key invariant: the cancellation fires only **after** `graceful_shutdown_timeout` elapses (see the `worker.py` docstring: "Seconds to allow in-flight activities to complete after SIGTERM before cancelling them"). If the process dies before that, no cancellation and no `WorkerEvicted` ever happen.

## Two outcomes, two retry costs

| Termination | Signal | Process lives to report? | Failure seen by server | Consumes `max_attempts`? |
|---|---|---|---|---|
| Rolling deploy, KEDA scale-down, `kubectl delete`, controlled drain | SIGTERM | Yes - pod has `terminationGracePeriodSeconds` (commonly 12h) | `WorkerEvicted` (non-retryable) | **No** - separate eviction budget |
| Spot / preemptible node reclaim | usually SIGTERM, but only seconds of grace | No - node reclaimed in ~30s-2min | none reported, then heartbeat timeout | **Yes** |
| OOMKill, SIGKILL, node/kubelet crash | none (SIGKILL) | No | none reported, then heartbeat timeout | **Yes** |

The dividing line is not the *cause* of the termination. It is whether the worker stays alive long enough to convert the cancel into a reported `WorkerEvicted`.

## Why spot reclaim consumes a retry

`terminationGracePeriodSeconds` is honoured by the kubelet for graceful pod eviction, but a spot or preemptible reclaim physically removes the node on the cloud provider's clock:

- AWS EC2 Spot: ~2 minute interruption notice
- GCP preemptible / Spot: ~30 second notice
- Azure Spot: ~30 second eviction notice

The worker's `graceful_shutdown_timeout` (default 3600s / 1h) cannot elapse inside that window, so the in-flight activity is never cancelled in-process and `WorkerEvicted` is never raised or reported. The activity simply stops heartbeating. Once `heartbeat_timeout` passes, the server fails the attempt with a heartbeat timeout, and a heartbeat timeout is retryable - it consumes one `max_attempts`. A run of these can exhaust `max_attempts` and fail the activity (and usually the workflow).

> Counterintuitive: a long `graceful_shutdown_timeout` is too long to help on spot. It is tuned for the long-grace rolling-deploy case. A timeout shorter than the interruption window would let the worker cancel and report `WorkerEvicted` in time, but only when the platform actually delivers a SIGTERM (node-termination handler, Karpenter, kubelet graceful node shutdown). Hard reclaims and OOMKills give no SIGTERM and no time, so they always fall through to heartbeat timeout.

## Can you distinguish a spot SIGTERM from any other SIGTERM?

Not from the signal. `SIGTERM` is signal 15 with no sender and no reason; a rolling deploy, a scale-down, a `kubectl delete`, and a spot drain all deliver the identical signal. The handler in `main.py` reads nothing about the cause - it only flips the shutdown flag. The SDK consults no cloud-metadata or node-state source.

The cause is only available out-of-band:

| Source | How | Notice |
|---|---|---|
| AWS IMDS | poll `169.254.169.254/latest/meta-data/spot/instance-action` | ~2 min |
| GCP metadata | poll `metadata.google.internal/computeMetadata/v1/instance/preempted` | ~30 s |
| Azure | poll `169.254.169.254/metadata/scheduledevents` for `EventType=Preempt` | ~30 s |
| Kubernetes | on SIGTERM, read the node's spot label / disruption taint via the API server | handler-dependent |
| Post-hoc | pod `lastState.terminated.reason` (`OOMKilled`, exit 137), node / Karpenter events | after the fact |

Important: distinguishing is not the bottleneck. The SDK already classifies *any* shutdown-flag cancel as `WorkerEvicted` - it does not need the cause. Spot consumes a retry only because the process dies before it can *report* the failure. Knowing "this is spot" helps only if the worker also acts faster: detect via IMDS, run a short shutdown, and rush the report inside the interruption window, while keeping the long grace for ordinary deploys. That behaviour is not implemented today, and it still cannot save a hard reclaim or OOMKill.

## What an eviction costs even when it does not consume a retry

A re-dispatch from `execute_activity_with_eviction_retry` is a brand-new `workflow.execute_activity` call with identical arguments. It starts at attempt 1 with **no Temporal heartbeat-resume** - heartbeat details (`activity.info().heartbeat_details`) only carry across retries of the *same* scheduled activity, never to a fresh re-dispatch. So an evicted long-running activity restarts from the beginning unless it checkpoints progress externally and reads it back on start. For activities that cannot finish within a termination window, external checkpointing is what prevents repeated restarts.

## Configuration

| Setting | Env var | Default | Defined in |
|---|---|---|---|
| Grace for in-flight activities before cancel | `TEMPORAL_GRACEFUL_SHUTDOWN_TIMEOUT` | 3600 s | `execution/settings.py` (`graceful_shutdown_timeout_seconds`) |
| Drain delay before worker shutdown (flush completions) | `ATLAN_SHUTDOWN_DRAIN_DELAY_SECONDS` | 5 s | `constants.py` (`SHUTDOWN_DRAIN_DELAY_SECONDS`) |
| Max eviction re-dispatches per activity call | `ATLAN_WORKER_EVICTION_MAX_RETRIES` | 3 | `constants.py` (`WORKER_EVICTION_MAX_RETRIES`) |

`graceful_shutdown_timeout` is overridable per deployment; some deployments set it to match a 12h `terminationGracePeriodSeconds` (see related RCA).

## Operational guidance

- Keep long-running, non-checkpointing activities off spot. Pin those workers to on-demand nodes (nodeSelector / taint). Spot is a poor fit for activities that cannot finish inside the interruption window, because every reclaim becomes a retry-consuming heartbeat timeout.
- Checkpoint activity progress externally for long work, so any restart - eviction re-dispatch or heartbeat-timeout retry - resumes instead of starting over.
- Match `terminationGracePeriodSeconds` to `graceful_shutdown_timeout` for graceful evictions so the worker can cancel and report `WorkerEvicted` before SIGKILL. This does nothing for spot, where the node is gone regardless.
- A heartbeat timeout in history with no preceding `WorkerEvicted` is the signature of an abrupt kill (spot, OOM, node loss), or of a worker that could not reach the server to heartbeat.

## Code references

- `application_sdk/main.py` - `_install_graceful_signal_handlers`: wires SIGINT/SIGTERM to set the shutdown flag
- `application_sdk/execution/shutdown.py` - process-wide shutdown flag (`is_worker_shutting_down`, `mark_worker_shutting_down`)
- `application_sdk/execution/_temporal/activities.py` - `except asyncio.CancelledError`: converts to `WorkerEvicted` when the flag is set
- `application_sdk/execution/retry.py` - `_with_worker_evicted_non_retryable`: always appends `WORKER_EVICTED_TYPE` to non-retryable types
- `application_sdk/execution/_temporal/eviction_retry.py` - `execute_activity_with_eviction_retry`, `_is_worker_evicted`: workflow-side re-dispatch loop
- `application_sdk/execution/_temporal/worker.py` - `AppWorker.__aexit__` (drain delay) and `graceful_shutdown_timeout` wiring
- `application_sdk/errors/leaves.py` - `WORKER_EVICTED_TYPE`
- `tests/unit/execution/test_eviction.py` - coverage of the eviction path end to end

## Related

- [RCA: Temporal worker shutdown deadlock - phantom task slot](./rca-shutdown-drain-delay-2026-03-27.md) - why the drain delay exists
