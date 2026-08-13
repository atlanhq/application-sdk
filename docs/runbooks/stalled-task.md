# Runbook: stalled task (`AtlanAppTaskStalled`)

**Alert:** `AtlanAppTaskStalled` — [`alerting/rules/App-Platform/atlan-apps-task-stall-alerts.yaml`](https://github.com/atlanhq/atlan-alerts/blob/main/alerting/rules/App-Platform/atlan-apps-task-stall-alerts.yaml)
in `atlanhq/atlan-alerts`.
**Signal:** `task_no_progress_gap_seconds` — the stall watchdog's no-progress gap
(see [ADR-0018](../adr/0018-progress-aware-heartbeat.md)).
**Severity:** `high`, `type: ticket`. Not a customer-visible outage on its own.

---

## What fired

A task in one app accumulated **at least an hour of unexplained silence per
hour** — an activity attempt that is *alive and heartbeating* but has reported no
observable unit of work (no batch written, no chunk transferred, no page fetched,
no `context.heartbeat()`) for one no-progress budget after another.

That is the *wedged-but-alive* shape. Everything else the platform already
detects: a crashed or OOM-killed worker stops beating and Temporal reclaims the
attempt at `heartbeat_timeout` (60s), and a failed attempt raises and shows up as
a workflow failure. A wedged-but-alive attempt emits nothing at all, which is why
it needs its own signal.

## Why a human is in this loop at all

While the fleet runs the stall watchdog in **`warn`** mode the SDK *reports* a
stall but never fails the attempt, and `start_to_close` is a **24h backstop**
rather than a tuned per-task ceiling. Before ADR-0018, that small tuned ceiling
was — accidentally — the only thing that killed a wedged activity. Raising it
removes that accident, so **this alert plus you is the containment** until the
app moves to `enforce` mode, where the SDK kills the attempt itself at
`max_no_progress_seconds` and the failure arrives as a normal
`TaskStalledError` workflow failure.

ADR-0018 → *Migration* states this as an accepted, measured regression. You are
the compensating control, and the thing you are containing is a **held worker
slot**: Temporal's Python worker has ~100 activity slots per pod, and every
wedged attempt occupies one for up to 24h.

!!! note "The alert reads the metric, not the log"

    The matching log line is deliberately **INFO**, not WARNING: warn mode is a
    fleet-wide default, so a single gap observation is an expected observation
    and not an actionable failure. Do not "fix" the log level to make stalls more
    visible — the alert threshold is what decides what is actionable.

## Read the alert first

| Label | What it tells you |
|---|---|
| `app_name` | The connector (process-wide app name, **not** the per-entry-point one) |
| `domainName` / `clusterName` | The tenant and its cluster |
| `task_name` | The `@task` that went quiet |
| `progress_last_label` | *Where* it went quiet — the last progress signal seen (e.g. `fetch_tables.page`, `writer.flush_buffer`, `run_in_thread.Cursor.execute`) |
| `watchdog_mode` | `warn` — reported only, nothing killed it. `enforce` — the SDK is already killing these attempts; repeated firing means it is re-stalling across retries |
| `$value` | Seconds of silence accumulated in the last hour. `3600` ≈ one permanently-silent attempt; `7200` ≈ two, and so on |

An **empty `progress_last_label`** is itself a finding: the attempt never emitted
a single progress signal, so it went quiet at the top of the task rather than
part-way through a loop.

The alert deliberately carries **no workflow run id** — a run id in a metric
label is an unbounded-cardinality bomb (see
[Metrics Standards](../standards/metrics.md)). Step 2 recovers it from the logs.

## Step 1 — is it still running?

Open the tenant's Temporal UI and list the app's running workflows:

```
https://<domainName>/temporal/namespaces/default/workflows?query=ExecutionStatus%3D%27Running%27
```

Sort by start time. A wedged attempt belongs to a run that has been open far
longer than that connector's usual runtime for that tenant.

If nothing is running any more, the attempt has already ended (it finished, was
retried, or hit a timeout). Nothing to contain — go to
[After the incident](#after-the-incident-the-durable-fix), because the gap is
still a real work-list item for the app team.

## Step 2 — find the run and the site

Search the app's logs for the watchdog's own line:

```
made no observable progress
```

Each occurrence carries the Temporal context the metric cannot: `workflow_run_id`,
`app_name`, plus the same task name, gap duration, budget and last progress label
in the message body. The repeat interval tells you a lot on its own:

- **Repeats every `max_no_progress_seconds` for the same run, still going** — one
  attempt, still alive, still silent. This is the wedge shape.
- **A handful of lines, then progress resumed** — a legitimate quiet spot that is
  longer than the budget. Not an incident; it is a work-list item.
- **Many runs each contributing one or two lines** — under-instrumented task, not
  one wedge. Same work-list item, no containment needed.

## Step 3 — classify

Three causes, in the order they actually occur:

| Evidence | What it is | What to do |
|---|---|---|
| Source system slow or unresponsive (its own dashboards, connection errors elsewhere in the log, other tenants on the same source affected) | **Source-side hang** — dropped connection, exhausted pool, socket read with no timeout. The most common cause, and the one the app never surfaced because the symptom is silence rather than an exception | Terminate and re-run once the source is healthy. A retry usually succeeds |
| One long opaque operation is genuinely expected here (a single large metadata query, one slow export API call), and `progress_last_label` names the site right before it | **Healthy-but-quiet**, missing instrumentation | Do **not** terminate. Note the site and go to [After the incident](#after-the-incident-the-durable-fix) |
| No source problem, gap keeps growing without bound, the run is well past any plausible duration for that tenant | **Genuine wedge** — infinite async retry loop, app-level deadlock, a fetch that streams forever | Terminate, then file a bug against the app with `task_name` and `progress_last_label` |

**If you cannot tell, wait one more budget rather than guessing.** A healthy quiet
spot ends; a wedge does not. One more `max_no_progress_seconds` (15 min at the
900s default) costs nothing against a 24h backstop, and "the gap is still
growing" is a far better tie-breaker than a coin flip — because terminating is
not free: unlike a stall kill in `enforce` mode, which fails the *activity* and
lets Temporal retry it, terminating kills the whole run and somebody has to
re-trigger it from zero.

## Step 4 — act

**Terminate the workflow run** from the Temporal UI (Terminate, not Cancel — a
wedged attempt will not observe a graceful cancellation any sooner).

What that does, precisely:

- The unconditional auto-heartbeat is what carries the termination back to the
  worker: the next beat (one `auto_heartbeat_seconds` interval — 10s unless the
  task overrode it) is answered with `NOT_FOUND`, and the activity is cancelled
  at its **next `await`**.
- An attempt wedged inside a **blocking call in a thread** has no next `await`
  and cannot receive that cancellation. The run is gone, but the thread and its
  worker slot are not reclaimed until the pod restarts. If slots are actually
  exhausted (`temporal_worker_task_slots_available` at 0 for that app), restart
  the worker deployment — that is the only lever that frees an orphaned thread.
- Terminating does not re-run anything, and it is **not** the retryable stall kill
  that `enforce` mode produces: that one fails the activity and Temporal retries
  it inside the normal retry budget. Re-trigger the workflow from the app's own UI
  or the platform's run UI once the cause is addressed.

Without checkpointing, that re-run restarts from zero — the same cost as any
`StartToClose` retry or manual re-run today, and the reason step 3 is worth the
few minutes it takes.

## After the incident: the durable fix

Every firing of this alert is also a work-list entry for the owning app team, and
the fix is a one-line observability change at the site named by
`progress_last_label`. Hand them
[Progress & Stalls → Reading your warn report](../concepts/progress-and-stalls.md#reading-your-warn-report),
which is the author-side version of this page: the same two metrics, read as a
ranked list with a triage table. In short:

- **A loop the SDK cannot see** — add `context.heartbeat()` or a
  `mark_progress()`-equivalent at the batch/page boundary. Never per record.
- **One opaque `await` against the source** — wrap it in
  `holding_progress("<site>", timeout=...)`. Expected in nearly every connector;
  the allowance is *how long you would let this one operation run before you
  would rather it failed*, sized from the site's observed p99 plus headroom, and
  err generous.
- **A blocking call through `run_in_thread`** — already auto-held, so it will not
  show up here as a gap at all. It shows up on the hold panels instead, as a long
  *unbounded* hold. Declaring an allowance there converts a 24h backstop wedge
  into one caught at `allowance + budget`.

An app that has worked through its work-list can then flip to `enforce`, verified
against a large-tenant/tail profile — at which point the SDK kills these attempts
in minutes and this runbook stops applying to it.

## If wedging turns out to be far more common than expected

Everything above assumes a wedge is rare enough that a human per firing is a
sensible control. ADR-0018 accepts that regression explicitly *because the wedge
rate was unknown* — warn mode is what measures it. If the measurement comes back
saying wedges are common, and worker slots are being burnt doing nothing, this is
the section you want.

**First, the counter-intuitive part: `ATLAN_PROGRESS_WATCHDOG=off` is not the
lever.** The resources are being burnt by the **24h backstop**, not by the
watchdog. The watchdog costs one metric per gap and is the only reason you can
see the problem at all; turning it off leaves every wedge exactly where it is and
blinds you to it. It is the switch for *"I want the telemetry gone"*, not for
*"wedges are eating my fleet"*.

| Lever (env var) | What it does | Reach for it when |
|---|---|---|
| `ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS=600` | The real revert — restores the pre-ADR-0018 ceiling and its ~30-min-per-dispatch worst case, including the accidental kill | You want the previous behaviour back, fleet-wide or for one app |
| `ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS=<seconds>` | Bounds the **retry product** (72h → whatever you set) while leaving one attempt generous | You want the blast radius capped but long single attempts still working |
| `ATLAN_PROGRESS_WATCHDOG=enforce` | The SDK kills each wedge itself at `max_no_progress_seconds`, collapsing the worst case from 72h to roughly 45 min | The wedges are visible as *gaps* **and** the affected apps are instrumented |
| `ATLAN_PROGRESS_WATCHDOG=off` | Stops gap reporting and enforcement. **Does not reduce wedge exposure** | Never, for this problem |

### Which lever depends on where the wedges are

The two shapes need different levers, and the metrics already tell them apart:

| What you see | Shape | What works |
|---|---|---|
| `task_no_progress_gap_seconds` firing | Wedged in async code or a quiet loop | `enforce` catches it one budget in |
| `task_hold_duration_seconds{hold_bounded="false"}` long, **no** gap metric | Wedged inside a blocking `run_in_thread` call | `enforce` does **nothing** — the unbounded auto-hold is vouching for it by design. Only the backstop revert or a `schedule_to_close` ceiling bounds these |

If the wedges are the second shape, do not reach for `enforce` and expect relief.

### Two things to know before you flip anything

- **All of these are read once at process start**, so any of them needs a **worker
  restart** to take effect. None is a live toggle. Plan the restart into the
  mitigation rather than discovering it mid-incident.
- **An explicit `@task(timeout_seconds=...)` beats the env var.** Apps that
  hard-code their own timeouts never took the 24h backstop, so the revert does not
  reach them — and they are not the apps whose exposure changed.

Whichever lever you use, record it: the wedge rate is the number ADR-0018 →
*Migration* says decides whether the fleet enforces, and a mitigation applied
without a note is a measurement lost.

## If you suspect a wedge and no alert fired

The alert is only as good as its inputs. In order of likelihood:

1. **The app's metrics never reach VictoriaMetrics.** A split-deployment worker
   pushes through a Pushgateway and needs `ATLAN_PROMETHEUS_PUSHGATEWAY_URL`
   set; unset, it logs a warning at startup and pushes nothing, and no
   task-level series exists for that app at all. Check with any always-emitted
   SDK series, e.g. `temporal_activity_executions_total{app_name="<app>"}`. If
   that is missing while activities are visibly running, fix the metrics path
   first — the stall alert cannot fire for that app.
2. **The watchdog is off for that task.** `watchdog_mode="off"` reports no gaps
   (its hold observations still record). Confirm on the dashboard's mode panel.
3. **A hold is vouching for the site.** An unbounded hold — every
   `run_in_thread` offload takes one — suppresses the stall watchdog for the
   whole call by design, so a wedged *blocking* call produces **no gap metric**.
   That residual is visible on the `task_hold_duration_seconds` panels
   (`hold_bounded="false"`, long durations), not on the gap panels. This is the
   documented cost of never false-killing legitimate blocking work.
4. **The gap is shorter than the budget.** A task that dribbles a progress signal
   every few minutes forever is never "stalled" by this definition. That is a
   run-duration problem; duration is an alert at the run level, not a kill (ADR-0018
   → *Bounding total time*).

## Panels

Import [`task-stall-dashboard.json`](../static/observability/task-stall-dashboard.json)
into Grafana (Dashboards → New → Import) against the VictoriaMetrics datasource,
or paste the individual queries into an existing app dashboard:

```promql
# Silence accumulating per task — the alert's own expression, ungrouped by tenant.
sum by (app_name, task_name, progress_last_label) (
  increase(task_no_progress_gap_seconds_sum[1h])
)

# Gap observations per hour, by watchdog mode: warn = reported, enforce = killed.
sum by (app_name, watchdog_mode) (increase(task_no_progress_gap_seconds_count[1h]))

# Work-list: long UNBOUNDED holds — the sites that want an explicit allowance.
topk(20,
  sum by (app_name, task_name, hold_label) (
    increase(task_hold_duration_seconds_count{hold_bounded="false"}[24h])
    - increase(task_hold_duration_seconds_bucket{hold_bounded="false", le="1000"}[24h])
  )
)

# Allowances that are too tight: holds the operation outlived.
sum by (app_name, task_name, hold_label) (
  increase(task_hold_duration_seconds_count{hold_lapsed="true"}[24h])
)
```

!!! warning "Read the hold percentiles carefully"

    `task_hold_duration_seconds` uses the OTel default bucket boundaries, whose
    resolution above 1000s is 1000 / 2500 / 5000 / 7500 / 10000. A
    `histogram_quantile()` over that range brackets a p99 very loosely, which is
    exactly the range that matters when sizing an allowance for a long blocking
    call. Prefer counting holds above the budget (as above) and reading
    `_sum / _count` for a mean, and treat any long-tail quantile as an
    order-of-magnitude hint.

## Threshold, and how to re-derive it

The rule fires on **`increase(task_no_progress_gap_seconds_sum[1h]) >= 3600`** —
one activity-hour of silence per wall-clock hour, per
`(tenant, app, task, last label, mode)`.

Why that shape:

- **It measures the exposure the ADR accepted**, which is silent-attempt-time
  holding worker slots. `3600` is one permanently-silent attempt; N concurrent
  wedges reach the threshold N times faster, so the page arrives sooner exactly
  when slot exhaustion is the real risk.
- **It separates a wedge from a merely-quiet spot by duration, which is the only
  honest discriminator.** Warn mode re-arms after each report, so a wedged
  attempt keeps contributing one budget of silence per budget for as long as it
  lives, while a legitimate long call contributes a bounded amount and stops.
  FND-165's evidence has apps needing 1800s windows for legitimate work, so any
  threshold under ~1h would page on healthy runs — the fleet-wide noise ADR-0018
  exists to reduce.
- **It costs latency, deliberately.** With the 900s fleet budget, a single wedge
  is *detected* at 900s (visible on the dashboard from the first gap) and *paged*
  at roughly 60 minutes. Against a 24h backstop that is the trade worth making.

If `max_no_progress_seconds` moves away from 900s, the threshold does **not**
need re-deriving — it is expressed in seconds of silence, not in gap counts. What
does change is the paging latency: it is always ~1h of accumulated silence, but
the first gap (and so the first dashboard evidence) arrives one budget in.

A single gap observation is **not** paged, by design. Those are the warn-mode
work-list, and they belong on the dashboard.
