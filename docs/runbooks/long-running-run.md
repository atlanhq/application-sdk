# Runbook: long-running run (run-length SLA)

**Signal:** `task_run_length_over_sla_seconds` — the age of a run observed past
`ATLAN_RUN_LENGTH_SLA_SECONDS` (see
[ADR-0018](../adr/0018-progress-aware-heartbeat.md) → *Bounding total time*).
**Condition:** `increase(task_run_length_over_sla_seconds_count[15m]) > 0`.
**Severity:** warning, `type: ticket`. Not a customer-visible outage on its own.

---

## What fired

A run has been going longer than the length its own app declared, and **nothing
will terminate it on time**. It is not stalled: it is making progress, or at least
making it often enough that the stall watchdog never sees a gap. It has not hit a
timeout either, because `start_to_close` is a 24h *backstop* per attempt, not a
budget for the run.

That is the shape no other signal catches:

| Shape | Caught by |
|---|---|
| Worker crashed, OOM-killed, node lost | `heartbeat_timeout` (60s), Temporal reclaims the attempt |
| Attempt alive but silent (wedged) | [`AtlanAppTaskStalled`](stalled-task.md) — the stall watchdog |
| Attempt failed | Workflow failure, activity error metrics |
| **Run long but progressing — or dribbling just enough progress to look like it** | **This alert, and only this alert** |

## Why a human is in this loop at all

The SDK cannot know how long a healthy run of an app takes against a tenant it has
never seen — that is precisely the number ADR-0018 exists to stop guessing. So the
duration *kill* was replaced with a duration *alert*, and the judgement it needs is
yours. An alert threshold that is wrong costs you one look; a kill threshold that is
wrong costs a tenant a run's worth of work.

Note the asymmetry with the stall alert: that one needs *accumulated seconds of
silence* because warn-mode gaps are normal fleet-wide. This series is emitted only
by a run already past its declared length, so a single observation is the finding.

## Read the alert first

| Label | What it tells you |
|---|---|
| `app_name` | The connector (process-wide app name) |
| `domainName` / `clusterName` | The tenant and its cluster |
| `task_name` | The `@task` that was executing when the run was observed over its SLA — where the run is spending its time, which is usually the interesting part |
| `temporal_workflow_type` | The workflow whose run is long |
| `$value` | The run's age in seconds. `histogram_quantile` over `_bucket`, or the `_sum`/`_count` ratio, answers "how far over" |

No workflow run id: a run id in a metric label is an unbounded-cardinality bomb
(see [Metrics Standards](../standards/metrics.md)). Step 1 recovers it from the logs.

## Step 1 — find the run

Search the app's logs for the run-length line:

```
run-length SLA
```

Each occurrence carries the Temporal context the metric cannot — `workflow_run_id`,
`workflow_id` — plus the age, the SLA and the task name in the message body. It is
logged **once per activity attempt**, so several concurrent long-running tasks in one
run produce one line each, and a run that outlives an attempt produces a fresh line
for the next one.

Then open the run in the tenant's Temporal UI:

```
https://<domainName>/temporal/namespaces/default/workflows?query=ExecutionStatus%3D%27Running%27
```

If nothing is running any more, the run finished on its own after the observation.
Nothing to contain — but go to [After the incident](#after-the-incident) anyway,
because either the SLA or the run's duration is still worth a decision.

## Step 2 — is it progressing?

This is the whole classification, and run length alone cannot answer it —
healthy-but-slow and wedged-but-dribbling look identical from age.

1. **The stall watchdog.** `task_no_progress_gap_seconds` for this app and task: gaps
   accumulating means it is going quiet in stretches, and the
   [stalled-task runbook](stalled-task.md) is the better tool. No gaps at all means it
   is genuinely reporting progress.
2. **The app's own throughput.** Records or batches written per minute, from the
   connector's own metrics or its logs. Flat at zero while progress signals continue
   is the dribbling shape: something is looping without doing work.
3. **The tenant's size.** A run that is 30 hours into a first full extraction of a
   very large source is a different finding from the same run against a source that
   took 40 minutes last week. Compare with `temporal_workflow_duration_seconds` for
   the same workflow type and tenant.

## Step 3 — act

| Evidence | What it is | What to do |
|---|---|---|
| Progress signals *and* throughput both moving; duration in line with the tenant's size | **Healthy but slow.** The SLA is the wrong number for this app, not the run | Do **not** terminate. Raise `ATLAN_RUN_LENGTH_SLA_SECONDS` for that app (see [After the incident](#after-the-incident)) and let the run finish |
| Progress signals moving, throughput flat, age growing without bound | **Wedged while dribbling** — a retry loop that never exits, a paginator that never advances, a poll with no terminal condition | Terminate the run, then file a bug against the app naming `task_name`. This is the failure mode the alert exists for |
| Gaps accumulating in `task_no_progress_gap_seconds` too | **A stall**, and the run length is a symptom | Follow the [stalled-task runbook](stalled-task.md) instead |
| Source system slow or unresponsive (its own dashboards, connection errors elsewhere in the log, other tenants on the same source affected) | **Source-side degradation** | Terminate and re-run once the source is healthy, or let it finish if the source is recovering |

**If you cannot tell, wait and re-check rather than guessing.** The observation
re-asserts every minute, so `$value` climbing tells you nothing new — but throughput
over the next 15 minutes does. Terminating is not free: it kills the whole run, and
without checkpointing the re-run restarts from zero.

**To terminate:** Temporal UI → Terminate (not Cancel). The unconditional
auto-heartbeat carries the termination back to the worker within one
`auto_heartbeat_seconds` interval, and the activity is cancelled at its next
`await`. An attempt wedged inside a blocking call in a thread has no next `await`;
the run ends but the worker slot is not reclaimed until the pod restarts. Terminating
does not re-run anything — re-trigger from the app's or the platform's run UI.

## After the incident

One of two durable outcomes, and "nothing" is not one of them — an alert nobody
resolves either way becomes noise, and this one is a judgement, not a fault:

- **The run was healthy.** The app's runs legitimately outlast 24 hours, so declare
  that: set `ATLAN_RUN_LENGTH_SLA_SECONDS` in the app's deployment env to a value
  above its real p99 run length, with headroom. Size it from
  `temporal_workflow_duration_seconds` for that workflow type — completed runs are
  exactly the distribution to read. Declaring the number is the point of the alert;
  it is not a workaround for it.
- **The run was wedged.** File the bug with `task_name` and the throughput evidence.
  If the task has an opaque long-running call in it, the durable fix is usually a
  progress hook or a `holding_progress(timeout=...)` allowance at that site — see
  [Progress and Stalls](../concepts/progress-and-stalls.md) — which converts the next
  occurrence from "long run, cause unknown" into a stall the watchdog names and, in
  `enforce` mode, kills without a human.

Consider also whether the task wants a ceiling on its retry product
(`schedule_to_close_seconds`) — see
[Tasks → Bounding the retry product](../concepts/tasks.md#bounding-the-retry-product).
A run whose length comes from three 24h attempts of the same wedge is a different fix
from one long attempt.

## Blind spots

The observation rides an activity's heartbeat tick, so a run is measured **only while
at least one of its tasks is executing**. Not covered:

- A run parked between activities (waiting on a signal, or on a child workflow whose
  own activities report under that child's run identity).
- A run whose worker is gone entirely — that is a worker-liveness condition
  (`ATLAN_WORKER_LIVENESS_MAX_IDLE_SECONDS`, `temporal_worker_task_slots_available`).
- A task with heartbeating disabled (`heartbeat_timeout_seconds=None`): no tick, no
  observation.

This is a deliberate trade. Measuring run length from inside workflow code would need
a durable timer in every run's history and would risk a non-determinism failure on
in-flight runs at every SDK upgrade. If one of the shapes above ever produces a real
incident, that timer — behind `workflow.patched` — is the escalation.
