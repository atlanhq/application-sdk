# Preflight outcome rows: name the failing check and its reason

- **Date:** 2026-09-22
- **Status:** Implemented. Revised after review — see *Revision* at the end.
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
2. `failure.check` = the name of the check `primary` describes, matched on
   `(code, message)`. A lone failed check is unambiguous and is named whether or
   not it matches. Several failed checks with no match — or several matching
   equally — has no answer, and the key is omitted rather than guessed.
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
needed: `FailureDetails` is already imported at `preflight_gate.py:73`, inside
the module's `workflow.unsafe.imports_passed_through()` block — a real runtime
import, not a `TYPE_CHECKING` one as an earlier draft of this document said. The existing
`audience` parameter is left alone — deriving it from `primary` instead would
be a wider signature change than this needs.

Every emit site passes the exact object its `reason` was derived from, or
`None` when `reason` is just the status: a block passes `_primary_failure()`
(as `block_error.details[0]`), a run that went ahead passes the new
`_proceeded_failure()` — which replaced `_proceeded_reason()` and returns the
object rather than only its code — a no-verdict row passes the rendered
verdict's primary, and the workflow frame passes the `evidence` it recovered off
the failure chain. `_failure_fields` derives nothing itself. Two earlier drafts
of this plumbing were wrong: the first passed nothing from the workflow frame
(the row named an unrelated advisory while `reason` named the real fault), the
second passed `verdict.error` and re-derived the rest, which was a second copy
of `_primary_failure`'s ladder with nothing keeping the two in step.

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
def redact_and_cap(text: str) -> str:
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
| workflow-frame block from recovered evidence | yes — matched by value, since the evidence crossed the wire |
| workflow-frame `gate_broken` fail-open | message only, when the plumbing envelope is readable; no check to name |

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

## Revision (2026-09-22, post-review)

An independent review found the original `c.error is primary` match wrong in two
reproducible cases, and it was: identity cannot survive a value object crossing
a serialization boundary.

1. The workflow frame passed no `primary` at all, so a block from recovered
   evidence named whichever check happened to fail first. With storage
   verification on and a killed attempt, the row read
   `reason=DEPENDENCY_UNAVAILABLE` beside `failure.check=version` — an unrelated
   advisory. Pointing a reader at the wrong check is worse than the blank row
   this change set out to fix.
2. A handler handing one `AppError` to both the aggregate and a check produces
   two distinct `FailureDetails`, because `PreflightOutput` and `PreflightCheck`
   coerce independently. Identity missed; so did `==`, because
   `_primary_failure` stamps `app_name` on the copy it returns.

Fixed by matching on `(code, message)`, by having the workflow frame hand over
the evidence it already holds, and by omitting `failure.check` when several
checks failed and none matches — a name that contradicts `reason` on its own row
is worse than no name. Both cases now have regression tests.

The original spec also mis-stated where the tests would live. They are in
`tests/unit/execution/test_preflight_gate_activity.py` (activity and interactive
rows) and `tests/unit/app/test_preflight_gate.py` (workflow frame). Putting none
in the workflow-frame module is why finding 1 shipped.

## Second revision (2026-09-22, after human review)

Five review points, all taken.

1. **`Body` searchability.** The spec's own evidence was a `Body`-substring
   search returning zero rows, and the first version of this change would not
   have altered that: the new keys are attributes. A preflight block is the one
   failure class carved out of the interceptor's `_failure_suffix`; every other
   failure gets its message into `Body`, a block logged only
   `<type> BLOCKED (preflight gate)`. The carve-out's stated intent was dropping
   the traceback and frame for a typed outcome, not the sentence. Both lifecycle
   lines now carry the block's first line after the token. The outcome row's own
   `Body` stays the constant event name — exact-match consumers filter on it.
2. **Redaction lives on the envelope.** `FailureDetails.message` and
   `suggested_action` are redacted by a `field_validator` where the envelope is
   built, covering every consumer at once. The emit-site call is now
   defence-in-depth plus the cap. `templates/sql_app.py` had already hand-rolled
   the same policy at another surface; the validator subsumes it.
3. **One attribution ladder per outcome shape**, above.
4. **`redact_and_cap`** is the one cap block — `sanitize_cause_repr` builds on
   it — and is no longer re-exported from `application_sdk.errors`; it has two
   consumers, both inside the package.
5. **A `frame_lost` block carries `failure.message`** with no `failure.check`:
   it has no checks and a fully populated primary. `gate_broken` still carries
   no message — `classify_gate_failure` returns no evidence for it by
   construction, so there is nothing to attribute; its diagnostic is the stack
   trace already attached.

One behaviour change fell out of (3) beyond the row fields: an interactive
`not_ready` row for an untyped check now stamps `failure.audience`, as the gate
row already did for the same shape. The two surfaces used to disagree.

Not folded in: `_failure_suffix` puts `str(exc)` into `Body` unredacted for
every non-preflight failure. Pre-existing, separate, and worth its own issue.

## Third revision (2026-09-22, reviewer follow-up)

Five cheap corrections, taken directly rather than through another review
round-trip. Four are comments and docs; one is behaviour.

**The behaviour one.** `preflight_block_message` read the block's *rendered*
message, which `_gate_error` composes as `"Preflight failed: <every failed
check's line, joined>"`. Two costs. The lifecycle line came out as `BLOCKED
(preflight gate): Preflight failed: …`, repeating the token it already carries.
And with more than one failed check the joined line names a check the outcome
row deliberately omits (see `_attributed_check`), so `Body` and
`failure.message` could carry different sentences for one block — the exact
thing the "one attribution ladder" revision existed to prevent, reintroduced on
a different surface.

It now prefers `details[0]` — the same `FailureDetails` the row's
`failure.message` comes off — and falls back to the rendered line only for a
`details[0]` it cannot parse, on the same tolerant terms as
`_gate_failure_evidence`: a newer producer's shape is not worth costing the
reader the sentence entirely. `test_the_lifecycle_body_and_the_row_carry_one_sentence`
pins the agreement against the real builder; a second test pins the fallback.

**The other four.**

- `app/base.py` passes `primary=failure.evidence` on the `GATE_BROKEN` branch,
  where it is `None` by construction. Kept, not deleted — it follows the same
  ladder as the other two branches and gains the sentence the day a broken gate
  learns to type itself — but now commented, because it reads as load-bearing.
- `templates/sql_app.py`'s `reconstruct` docstring claimed the envelope never
  redacts `message` / `suggested_action`. The `field_validator` added in this
  change makes that false, and it runs on `model_validate` too, so a replayed
  envelope arrives scrubbed. The re-redaction there is now defence in depth for
  a hand-built `PrimeAuthOutput`; `cause_repr` and nested evidence values are
  what the envelope genuinely still does not cover.
- A **soft**-mode `would_block` raises no block, so it has no lifecycle record
  and no `Body` line — reachable through `failure.message` only. Acceptable for
  an advisory outcome, but it was an unstated gap. Now stated, in
  `docs/concepts/apps.md` and `docs/agents/coding-standards.md`.

**Follow-up finding, same pass.** `emit_preflight_check_outcome` took `primary`
off `_proceeded_failure` and then set `reason = result.status.value` anyway, so
an interactive row that went ahead with a failed check reported `partial` while
the gate row reported that check's code for the identical verdict — and
`failure.check` / `failure.message` / `failure.audience` on the interactive row
came off an object its own `reason` did not. That is the split this revision
existed to close, surviving inside the function that closes it.

`reason` is now `primary.code` whenever there is a primary, on both surfaces.
The status is not lost: it is on the same row under `outcome`, which is where a
consumer filtering for partials should already be looking. A row with nothing
failed has no primary and keeps the status as its reason.

The behaviour was pinned by `test_partial_keeps_status_reason_but_stamps_audience`
from #3492, whose name asserted the old intent; it is now
`test_partial_reason_names_the_failed_check_and_stamps_audience`, with
`test_a_clean_row_keeps_the_status_as_its_reason` beside it for the no-primary
half, and a `reason` assertion added to the gate/interactive agreement test.
Reverting the one line turns two of the three red.

## Fourth revision (2026-09-22, after an independent fit-for-purpose review)

The review judged the change against the ticket's acceptance criterion — could
support's original search now find the reason — and answered *partly*. Every
defect it reported reproduced.

1. **Row and `Body` could disagree** for an un-migrated handler that set
   `result.message` and marked a check failed with no text of its own:
   `_primary_failure` never read `result.message`, so the row said "Preflight
   check failed" while `Body` carried the real sentence. The second revision's
   claim that the four keys "cannot disagree" was false. One `_fallback_message()`
   now feeds the untyped `details[0]` and the raised error's message. Together
   with the third revision's `Body`-from-`details[0]`, the row and `Body` carry
   one sentence for every verdict shape; only the raised error's own message
   lists every failed check's line when several failed.
2. **The envelope validator over-redacted fleet-wide.** The userinfo pattern
   was greedy to the last `@` by design, written for log strings; on the
   Automation Engine-facing `message` it wiped `abfss://container@account` and
   query-string e-mail addresses. It now requires a password (`user:pass@`) and
   stops at the first `/`. An earlier test pinned the greedy behaviour as
   intentional; it is rewritten to pin the reversal, with the reason.
3. The raised error's message — the `exception.message` on the adjacent record —
   was built from raw handler strings and never redacted. It is now.
4. An un-migrated check whose own line is the attribution is named; two
   untyped lines joined still name nothing.
5. The `BLOCKED` line's `Preflight failed:` prefix: a strip was written, then
   dropped — the third revision's `details[0]` read has no prefix to begin with,
   and on the details-less fallback the rendered line is kept whole by design
   ("cost the reader the prefix, not the sentence").
6. The workflow's `App blocked by preflight gate` record logged `str(e)` — the
   wrapper's text. It now carries the block's line.
7. `gate_broken` **does** have something to attribute: `_plumbing_error` leaves
   `FailureDetails` at `details[0]`, and `_gate_failure_evidence` simply never
   read it. The second revision's "nothing to attribute" was wrong, and so is
   the third revision's note that `primary=failure.evidence` is `None` by
   construction on that branch — it is the plumbing error's `details[0]` when
   readable. The row now carries the plumbing failure's message.
8. `failure.suggested_action` is added. The ticket's search included the
   remediation text; the first revision's "the message alone closes the support
   gap" was contradicted by the ticket's own evidence.

What support still cannot find by a `Body` search: the wire type name
`PreflightFailed` (the token `BLOCKED (preflight gate)` is the searchable form of
that fact), and anything past the first line or the 200-character cap of a
block's message (`failure.message` on the outcome row keeps the full text). The
customer-facing log view filters at ERROR; the `Body`-carrying lifecycle lines
are WARNING, and the ERROR record's `Body` remains the constant event name.
