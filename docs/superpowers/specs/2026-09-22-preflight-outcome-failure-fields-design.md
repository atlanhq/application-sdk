# Preflight outcome rows: name the failing check and its reason

- **Date:** 2026-09-22
- **Status:** Approved, not yet implemented
- **Branch:** `feat/preflight-outcome-failure-fields` (off `origin/main` @ `0e864d4c`)
- **Origin:** CONNECT-1821

## Problem

When the preflight gate blocks a run, no single log record answers "which check
failed, and why".

The `Preflight gate outcome` row carries `reason`, which is a *code*
(`PERMISSION_DENIED`), and `check_matrix`, which is JSON naming each check and
its `error_code` but carrying no message. The human sentence lives on a
different record — `Completing activity as failed` — under
`LogAttributes['exception.message']`.

On CONNECT-1821 that cost a production escalation several days. Support searched
`otel_logs.service_logs` for the endpoint and the error text, got zero rows
(the strings are in `LogAttributes`, never in `Body`), and concluded the reason
was not logged at all. It was logged; it was one record away, under a key
nobody thought to search. The ticket was filed as data loss. It is not data
loss — it is a shape problem, and the shape is ours.

Verified against the escalation's live ClickHouse records:

```
Body         = "Preflight gate outcome"           (22 chars)
reason       = "PERMISSION_DENIED"
check_matrix = [... {"name":"scannerApiAvailability","passed":false,
                     "error_code":"PERMISSION_DENIED","duration_ms":47.662}]

-- adjacent record, Body = "Completing activity as failed"
exception.message = "PreflightFailed: Preflight failed: The Power BI admin API
                     (admin/workspaces/modified) returned 403. ..."
```

A tenant-wide `Body`-substring search for the endpoint, the error type and the
remediation text over the incident window returns **0 rows**. The same window
held 27 records carrying `check_matrix` and 2 carrying the full message under
`exception.message`.

## Goal

One record, one query, answers "which check, and why" — for both surfaces that
emit a preflight outcome.

## Non-goals

- Changing `reason`. It is a code so dashboards can separate fault classes;
  turning it into a sentence would break that.
- Putting messages inside `check_matrix`. [preflight_gate.py:960][cm] documents
  the rule — *"Small fixed fields only — messages and evidence stay in the
  Temporal activity result"* — because connector-pulse `JSONExtract`s that
  field. Row size would then scale with handler-authored text.
- `failure.suggested_action` as a separate field. Considered and deferred; the
  message alone closes the support gap.
- The fail-open `exc_info` gap (separate open Linear issue). Different code
  path — that one is gate plumbing failing open, this one is a clean verdict
  failing closed.
- The `preflight result not persisted` DNS failure seen in the same
  escalation. Real, but it is a deployment gap (`system-workflows` was not
  running on that tenant), not an SDK change.

## Design

### New attributes

Two conditional keys on both outcome rows:

| Key | Value |
|---|---|
| `failure.check` | name of the check that caused the verdict |
| `failure.message` | its human line, secret-redacted and length-capped |

`failure.` is already a passthrough prefix in
[logger_adaptor.py:284][pass], so both keys reach OTLP and ClickHouse with **no
edit to `_KNOWN_EXTRA_KEYS`**, and they sit beside the `failure.audience` those
rows already carry.

They are *conditional*, so they do **not** join `GATE_OUTCOME_ROW_KEYS` — that
tuple is the always-present contract, and `FAILURE_AUDIENCE_KEY` is already
excluded from it for the same reason. The existing
`set(GATE_OUTCOME_ROW_KEYS) <= row.keys()` assertions stay green.

### The helper

One module-level function in `preflight_gate.py`, beside `_check_matrix_json`.
It is referenced by [`gate_outcome_row`][row] at line 702, i.e. before its own
definition at ~960 — the same forward reference `_check_matrix_json` already
relies on, resolved at call time, not import time:

```python
def _failure_fields(
    checks: list[PreflightCheck], primary: FailureDetails | None
) -> dict[str, str]:
    """The failing check's name and human line, for the outcome rows."""
```

Derivation, mirroring `_build_block_error`'s precedence exactly so the row can
never name a different cause than the error actually raised:

1. `failed = [c for c in checks if not c.passed]` — empty → return `{}`
2. `failure.check` = the name of the check whose `.error is primary`; else
   `failed[0].name`. The fallback covers the handler-aggregate case, where
   `result.error` is pinned on the verdict and belongs to no single check.
3. `failure.message` = `primary.message` if `primary` else
   `failed[0].resolved_message` (the property already encodes the
   `error` -beats- `message` precedence).
4. Each key is included only when its value is non-empty, so a failed check
   with no text contributes nothing rather than an empty attribute.

### Wiring

Two emit paths, one helper.

**Gate row** — [`gate_outcome_row`][row] is the single row-shape builder used by
both frames that emit `Preflight gate outcome`
([preflight_gate.py:2080][act], the activity frame; [app/base.py:2582][wf], the
workflow fail-open frame). It gains one optional parameter:

```python
primary: FailureDetails | None = None
```

and merges `_failure_fields(checks, primary)` into the row. No new import is
needed: `FailureDetails` is already a `TYPE_CHECKING` import at
`preflight_gate.py:73`, and the module has `from __future__ import annotations`,
so the annotation never evaluates at runtime. The existing
`audience` parameter is left alone — deriving it from `primary` instead would
be a wider signature change than this needs.

Both callers already hold the value: they pass
`audience=block_error.details[0].audience.value`, so they pass
`primary=block_error.details[0]` alongside it. The workflow fail-open frame
often has no checks at all, in which case the helper returns `{}` and the row
is unchanged.

**Interactive row** — the `Preflight check outcome` emit at
[preflight_gate.py:1262][int] already computes `primary` on the line above its
`extra` dict. It merges `_failure_fields(result.checks, primary)` into `extra`.

### Redaction: required, not defensive

`FailureDetails.message` is handler-authored and **not** sanitized. Verified on
main at [errors/base.py:286][td]:

```python
message=self.message,                                        # raw
cause_repr=sanitize_cause_repr(self.cause) if self.cause else None,   # sanitized
```

A driver exception routinely carries a connection string, so copying `message`
onto a widely-read row without scrubbing would be a real leak.

New helper in `errors/base.py`, beside the existing ones:

```python
def redact_and_cap(text: str, max_len: int = _CAUSE_MAX_LEN) -> str:
    """Redact secrets, then head+tail truncate."""
```

Redaction runs **before** truncation — FND-957's rule, so a retained tail can
never expose an unredacted secret. Truncation keeps both ends for the same
reason `sanitize_cause_repr` does: a backend error puts the URL at the head and
the reason at the tail. Reuses `redact_secrets` and `_CAUSE_MAX_LEN = 2000`.

`sanitize_cause_repr` keeps its own body. It works, it is tested, and folding it
into the new helper is a refactor this change does not need.

## Blast radius

Additive only. `reason`, `outcome`, `check_matrix`, `gate_mode`,
`gate_classification`, `failure.audience` are untouched, so existing dashboards
and connector-pulse `JSONExtract` queries keep working.

Rows that gain the keys — any outcome with at least one failed check:

| Outcome | Gains keys? |
|---|---|
| `blocked` (hard mode, NOT_READY verdict) | yes |
| `would_block` (soft mode, same verdict) | yes |
| `blocked` / `would_block` via `source_unverifiable` | yes — one synthesized failed check, `UNVERIFIABLE_CHECK_NAME` |
| `proceeded` with advisory failed checks | yes |
| `proceeded`, all checks passed | no |
| `skipped` | no |
| workflow-frame fail-open with no checks | no |

Both rows derive from one helper, so the two surfaces cannot drift — the
property [`gate_outcome_row`][row]'s own docstring already asks for.

## Testing

TDD, red first. Gate-row tests in `tests/unit/app/test_preflight_gate.py` and
`tests/unit/execution/test_preflight_gate_classification.py`; interactive-row
tests in `tests/unit/execution/test_sdr.py` (the SDR surface) and
`tests/unit/handler/test_service.py` (the HTTP surface).

1. Blocked verdict → `failure.check` is the failing check's name,
   `failure.message` its message
2. Handler aggregate `result.error` set *plus* a failed check → name from the
   check, message from the aggregate
3. `proceeded` carrying an advisory failure → both keys present
4. Clean `proceeded` → neither key present
5. Message containing `password=hunter2` → lands redacted
6. Message longer than `_CAUSE_MAX_LEN` → capped, head and tail both retained
7. `source_unverifiable` path → `failure.check == UNVERIFIABLE_CHECK_NAME`
   (now imported from `application_sdk.handler.contracts`)
8. Gate row and interactive row emit the same two keys for the same verdict
9. Existing `set(GATE_OUTCOME_ROW_KEYS) <= row.keys()` assertions still pass

## Result

The blocked record becomes self-sufficient:

```
Body            = "Preflight gate outcome"
outcome         = blocked
reason          = PERMISSION_DENIED
failure.audience= USER
failure.check   = scannerApiAvailability
failure.message = The Power BI admin API (admin/workspaces/modified) returned
                  403. The service principal lacks read-only admin API access.
check_matrix    = [...]
```

[cm]: ../../../application_sdk/execution/_temporal/preflight_gate.py#L960
[row]: ../../../application_sdk/execution/_temporal/preflight_gate.py#L702
[act]: ../../../application_sdk/execution/_temporal/preflight_gate.py#L2080
[int]: ../../../application_sdk/execution/_temporal/preflight_gate.py#L1262
[wf]: ../../../application_sdk/app/base.py#L2582
[pass]: ../../../application_sdk/observability/logger_adaptor.py#L284
[td]: ../../../application_sdk/errors/base.py#L286
