# ADR-0022: Skipped-Record Capture — One Helper, One File per Run, Status from the Count

## Status
**Accepted**

## Context

An app sometimes skips a record on purpose. The motivating case is P028's `creator-blank-guard`: a
record whose qualifiedName input is blank (empty string or `None`) would publish a broken identity
such as `{conn}//{schema}`, so the record is skipped. The rule owner's decision was that skipping is
right but **a log line is not enough** — the skipped record must be kept where a person can review
it across the whole run.

The SDK had the pieces but no path: `OutputStatus.PARTIAL_SUCCESS` names this case in its docstring
but stores nothing, `get_outputs()` carries summed metrics and artifact references, and nothing wrote
a file of skipped records. Left to each app, the fleet would drift into as many formats, locations and
reason vocabularies as there are apps.

## Decision

One public helper, `await skip_record(record, reason, *, record_id, entity_type, detail=None)`
(`application_sdk.outputs`), called from inside a `@task`'s per-record loop. Everything else is the
SDK's job.

| Stage | What happens | Where |
| --- | --- | --- |
| Each call | One NDJSON line `{record_id, entity_type, reason, detail, record}` is buffered and written to a local file **off the event loop**; `skipped-records` and `skipped-records-<reason>` metrics are added; one warning names `record_id`, `entity_type` and `reason` only | `outputs/skipped.py` |
| Activity end | The activity's file is uploaded as a part: `{run prefix}/skipped-records/parts/{activity_id}.ndjson` | output activity interceptor |
| Run end | Only when the summed `skipped-records` > 0: the framework task `merge_skipped_records` concatenates the parts into `{run prefix}/skipped-records.ndjson` and adds it as the artifact `skipped-records` | `App._run` → `App.merge_skipped_records` |
| Workflow exit | A `SUCCESS` result becomes `PARTIAL_SUCCESS` when `skipped-records` > 0 | output workflow interceptor |

**Absence is the signal.** A run that skips nothing has no file, no artifact, no `skipped-records`
metric, and keeps its status. A reviewer does not open an empty file to learn there was nothing to
review.

**Reasons are a closed set.** `SkipReason` is an SDK enum (`BLANK_IDENTITY`,
`MISSING_REQUIRED_FIELD`, `MALFORMED_RECORD`, `UNSUPPORTED_TYPE`); a free string raises `TypeError`.
Specifics go in `detail`, which reaches the file only. Fleet-wide aggregation by reason is only
possible if every app draws from the same vocabulary; adding a member is an SDK change on purpose.

**Customer data stays in the tenant store.** The record is source data. It is written to the
object store under the run prefix, next to the raw output, and never to logs or telemetry: those get
the caller-supplied `record_id` and counts only.

**Bounding is ADR-0020's two tiers.** Counts are unbounded. The two output surfaces — lines in the
merged file and per-record warnings — are capped by one constant, `MAX_SKIPPED_RECORDS` (10,000), so
they cannot disagree. Every skip lands in exactly one of: a line in the file,
`skipped-records-truncated`, or `skipped-records-unpersisted`.

**Storage fails open.** A part upload or merge failure is logged at ERROR and counted in
`skipped-records-unpersisted`; the counts and the status still land. Failing the activity would
re-run the whole extraction to recover a review file.

**Status is derived, not carried.** `PARTIAL_SUCCESS` comes from the summed metric that already
travels on activity results, so no new field crosses Temporal history. `FAILURE` and an app-set
`PARTIAL_SUCCESS` are never changed, and task-level outputs are never touched.

**Replay-safe.** The merge task is scheduled only when the count is above zero. A run started on an
older SDK cannot carry that metric, so its history never gains the new activity on replay.

## Sync mappers: `RecordSkipped` (FND-4963)

`skip_record` is async because it writes off the event loop, but the canonical mapper is a plain
`def`: a pure transform over sync pyatlan creators. Making mappers async to reach `skip_record`
would add nothing. Instead **the mapper detects, the loop records**: the mapper raises a leaf of
`RecordSkipped` and the `@task` loop catches it and awaits `skip_record`.

```python
def map_table(row: Row, connection_qn: str) -> Table:      # stays sync and pure
    if not row.schema_name:
        raise BlankIdentity(row, record_id=row.table_id, entity_type="Table",
                            detail="schema name is blank")
    return Table.creator(...)

for row in rows:                                           # @task loop
    try:
        asset = map_table(row, connection_qn)
    except RecordSkipped as skip:
        await skip_record(skip.record, skip.reason, record_id=skip.record_id,
                          entity_type=skip.entity_type, detail=skip.detail)
        continue
```

* **One leaf per `SkipReason`, reason on the class.** `BlankIdentity`, `MissingRequiredField`,
  `MalformedRecord`, `UnsupportedType`. As ADR-0013 does for error codes, a value that never varies
  per instance is a ClassVar; the base `RecordSkipped` cannot be raised. The loop catches the base
  and reads `skip.reason`, so one `except` covers every reason, and a test pins one leaf per reason.
* **The loop catches only `RecordSkipped`.** A creator's `ValueError` for a value the mapper did
  not guard still fails the task. Catching it per record would make the asset vanish with no guard
  naming why.
* **It carries the record.** Everything `skip_record` needs travels on the exception, including the
  record, because a mapper may skip a nested record (one column of a table row, one path of a spec)
  that the loop never holds, and the file must keep the record that was actually skipped. The
  record is kept out of `args`, `str()` and `repr()`, so tracebacks, logs and Temporal failure text
  name only `record_id`, `entity_type` and `reason`. It is picklable, for mappers run in a process
  pool.
* **Not an `AppError`.** A skip is a decision, not a failure: it has no category, owner or retry
  policy, and a generic `except AppError` must never treat it as one.
* **Uncaught fails visibly, once.** A `RecordSkipped` that escapes the `@task` is an app bug and
  deterministic, so the activity boundary fails it as a non-retryable `InternalError` naming the
  ids, the reason and the fix, rather than burning the retry budget on the same outcome.

Rejected: a **sync `skip_record`** (needs thread-safety work for mappers run through
`run_in_thread`, and a mapper with side effects is no longer pure), and an **async helper that calls
the mapper and records the skip** (hides what the loop catches; the explicit `try/except` is four
lines).

## Relationship to `AssetValidationReport`

They answer different questions about different records. `AssetValidationReport` (ADR-0020) is the
SDK checking assets an app **did emit** and reporting the ones that look wrong, warn-only; the app
did not choose those failures. `skip_record` is the app declaring a record it **chose not to emit**,
and why. They share the bounding rule and the summed-count-plus-capped-detail shape, not a report
type: a skip is a decision, not a finding.

## Options Considered

* **One artifact per activity** (`skipped-records-<activity_id>`) — no merge task, but a fan-out of
  N activities lists N artifacts and the reviewer stitches them together. Rejected.
* **One artifact pointing at the parts prefix** — no merge task, but every consumer of run artifacts
  would need to accept a prefix where today it reads a file. Rejected.
* **Fail the activity on upload failure** — the review file is never silently incomplete, at the
  cost of re-running the activity body. Rejected in favour of fail-open with an explicit
  `skipped-records-unpersisted` count.

## Consequences

* Apps get one call and no storage code; a sync mapper raises a `RecordSkipped` leaf for the loop
  to record. The `migrate-asset-modeling` `creator-blank-guard` default
  can become "skip via `skip_record`".
* Runs that skip anything gain one short framework activity at the end.
* The object-store layout and metric names are a cross-repo contract
  (`docs/standards/cross-repo-contracts.md`).
