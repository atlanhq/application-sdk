# RCA: Temporal Worker Shutdown Deadlock — Phantom Task Slot

**Date:** 2026-03-27
**Component:** application-sdk (`Worker._shutdown_worker`)
**Impact:** Worker pod stuck in Terminating state for 12 hours, blocking workflow processing
**Fix:** Add event loop drain delay before `worker.shutdown()`

---

## Incident Summary

An automation-engine worker pod remained in `Terminating` state for 12 hours after receiving SIGTERM. During this time, the Temporal worker held a phantom "in-use" task slot that could never be released, blocking `worker.shutdown()` from completing until the `graceful_shutdown_timeout` (12 hours) expired. Other workflows queued behind this worker experienced delays of up to 39 minutes.

## Root Cause

A race condition between SIGTERM signal handling and the Temporal SDK's activity completion flow.

### What happened

1. The worker received an activity (`save_workflow_run_state`, attempt 2)
2. The activity failed — Atlas API rejected the payload (`appWorkflowRunDag exceeds limit of 100000 characters`)
3. **3 seconds later**, SIGTERM arrived (deployment rollout)
4. The signal handler called `asyncio.create_task(self._shutdown_worker())`
5. `_shutdown_worker()` called `self.workflow_worker.shutdown()` immediately
6. `shutdown()` stopped all pollers and waited for in-flight task slots to drain

### Why it deadlocked

The Temporal Python SDK processes activity completions in an async coroutine (`_run_activity` in `temporalio/worker/_activity.py`). After an activity function raises an exception, `_run_activity` must:

1. Encode the failure into a completion protobuf (line 360)
2. Await the last heartbeat task (line 383)
3. Call `await self._bridge_worker().complete_activity_task(completion)` (line 392) — this sends `RespondActivityTaskFailed` to the Temporal server
4. Remove the activity from `_running_activities` (line 393) — this frees the task slot

The SIGTERM signal handler created a new asyncio task (`_shutdown_worker`) which competed with the `_run_activity` coroutine on the event loop. Since `_shutdown_worker` had no `await` before calling `shutdown()`, it ran to `shutdown()` without yielding, changing the SDK's internal state before `_run_activity` could reach `complete_activity_task()`.

### Evidence from production metrics

Metrics from the stuck worker pod (`automation-engine-worker-8696997456-z2rwj`):

```
temporal_num_pollers{activity_task}:        0    (stopped by shutdown)
temporal_num_pollers{sticky_workflow_task}:  0    (stopped by shutdown)
temporal_worker_task_slots_used{ActivityWorker}:  1    (phantom — never freed)
temporal_worker_task_slots_used{WorkflowWorker}:  1    (phantom — never freed)
temporal_sticky_cache_size:                       1    (workflow stuck in cache)
temporal_request{RespondActivityTaskFailed}:       (absent — never sent)
temporal_request{RespondWorkflowTaskCompleted}:    (absent — never sent)
```

The worker had zero pollers (shutdown stopped them) but still held 1 activity + 1 workflow slot as "in-use". Zero `Respond*` RPCs were ever made — confirming the activity completion was preempted.

### Timeline

| Time (UTC) | Event |
|---|---|
| 11:14:53 | Worker pod `z2rwj` starts |
| 11:15:05 | Activity `save_workflow_run_state` (attempt 2) received and executed — Atlas error |
| 11:15:08 | SIGTERM received — `_shutdown_worker` task created |
| 11:15:08 | `worker.shutdown()` called — pollers stopped, waiting for in-flight slots |
| 11:15:08+ | `_run_activity` never reaches `complete_activity_task()` — slot never freed |
| 12:46:26 | Temporal server-side terminates the workflow (heartbeat timeout) |
| 12:46:26+ | Worker never learns about termination (pollers stopped) |
| ~23:15 | `graceful_shutdown_timeout` (12h) would expire — pod finally exits |

## Fix

Add an `asyncio.sleep(SHUTDOWN_DRAIN_DELAY_SECONDS)` before calling `worker.shutdown()` in `_shutdown_worker()`. This yields the event loop, allowing any pending `_run_activity` coroutines to reach `complete_activity_task()` and flush their RPCs before shutdown changes the SDK state.

### Why `asyncio.sleep` is correct here

The sleep doesn't need to wait for the gRPC call (`complete_activity_task`) to finish — that's what `graceful_shutdown_timeout` handles. It only needs to let `_run_activity` get **scheduled** on the event loop to reach the `complete_activity_task()` call. This is a microseconds operation; the 5-second default is massively overprovisioned.

Once `complete_activity_task()` is called, the Rust core knows about the completion. `worker.shutdown()` will then properly wait for the gRPC response and release the task slot, all within the existing `graceful_shutdown_timeout`.

### Changes

**`application_sdk/constants.py`**
- Added `SHUTDOWN_DRAIN_DELAY_SECONDS` (default: 5, configurable via `ATLAN_SHUTDOWN_DRAIN_DELAY_SECONDS`)

**`application_sdk/worker.py`**
- `_shutdown_worker()`: added `await asyncio.sleep(SHUTDOWN_DRAIN_DELAY_SECONDS)` before `worker.shutdown()`

**`tests/unit/test_worker.py`**
- `TestShutdownDrainDelay`: 4 new tests that reproduce the race condition and prove the fix:
  - `test_without_fix_activity_completion_is_preempted` — with delay=0 (old code), the activity completion never runs. **Reproduces the bug.**
  - `test_with_fix_activity_completion_runs_before_shutdown` — with delay>0 (new code), the activity completion runs first. **Proves the fix.**
  - `test_fix_flushes_multiple_pending_completions` — multiple pending completions all flush
  - `test_shutdown_completes_even_with_zero_delay` — no regression when no in-flight work

## Contributing Factors

These are separate issues that amplified the impact but are not addressed in this PR:

1. **KEDA ScaledObject paused** (`autoscaling.keda.sh/paused: "true"`) — manually annotated since March 19, preventing autoscaling. Single worker had to process all workflows serially.

2. **Atlas `appWorkflowRunDag` 100K character limit** — the activity that triggered the race fails deterministically for large DAGs. All 3 retry attempts fail with the same error.

3. **`terminationGracePeriodSeconds: 43230`** (12 hours) — matches `graceful_shutdown_timeout` but means the pod lingers in Terminating state for the full duration of the deadlock.
