---
kind: responsibility
name: error-handling-area
description: >
  Maintains the current E-series violation-set and drives remediation of
  error-handling conformance findings.  The only fully-implemented area in
  phase 1.
---

### Maintains

The current set of unsuppressed E-series (error-handling) conformance findings
in the working tree, classified by disposition (FAILING / WARNING) and
remediability.

#### violations-error-handling

The fingerprint-set of all unsuppressed FAILING E-series results in the
current working tree, as reported by `suite.runner --series E`.

In strict mode the fingerprint-set extends to include unsuppressed WARNING
results as well.

This facet's fingerprint moves when any E-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series E` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).
- `rule_ids` — optional list of exact rule IDs (propagated from the
  top-level entry). Forwarded verbatim into every runner invocation this
  area makes — the loop's detect calls and the suggest-only
  `detect-violations` calls alike — so a `--rule`-scoped run stays scoped
  here rather than silently widening to the whole series at this hop.

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
This is the Reactor-ready wake source — in the Claude Code skill path, the
skill caller re-invokes on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "E"
  rule_ids: rule_ids
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "error-handling"`._

Consult the finding's `hint` and `message`, then look at the actual source
lines around `finding.line` in `finding.file` before proposing a fix.

#### Credential-boundary contraindication — read before adding `exc_info=True`

The raw exception from a database driver, an HTTP client or an auth call can
embed credentials: a JDBC URL carrying a password, an `Authorization` header
or HMAC, connection properties, an OAuth response body.  `exc_info=True`
serialises the traceback *separately*, so it bypasses whatever redaction the
message itself performs — adding it at such a site **creates a leak that was
not there before**.  A production security review over the fleet remediation
(FND-57) found exactly this shape in five connector repos.

None of this applies to a log call within the three statements before a
`raise` in the same block (an L009 site): there the line is deleted or
downgraded to a sanitized DEBUG, never enriched (see the E005 carve-out
below).

So before adding `exc_info=True`, or adding a log call that formats the
exception, ask what the caught exception can carry.  If the `try` wraps a
connect, an authenticate, a token refresh, or any request whose URL, headers
or body hold a secret, **do not add `exc_info=True`**.  Log through a
redaction helper instead:

```python
from application_sdk.errors import redact_secrets, sanitize_cause_repr

logger.error("connect failed: %s", sanitize_cause_repr(exc))
logger.warning("token refresh failed: %s", redact_secrets(str(exc)))
```

**This clears the rule, and needs no suppression.**  E004, E005 (and L004 in
the logging area) all accept a log call whose arguments flow through a
sanitizer as a deliberate no-traceback boundary — see
`suite/checks/_ast_common/_sanitizers.py`.  The redacted form is a
first-class fix, not a carve-out.  For E004 the call must also be at
`warning`, `error` or `critical`: a sanitized `debug`/`info` line still
fires it.

Three things to get right, because all three fail silently:

- **Recognition is by name.**  The callable must contain `redact`,
  `sanitiz`, `scrub_secret`, `mask_secret` or `safe_traceback`, or the
  argument must be a variable named for redacted output (`safe_traceback`,
  `redacted`, `sanitized`, `masked`, `scrubbed`).  `redact_secrets`,
  `sanitize_cause_repr`, `safe_traceback` and `redact_wire_value` are public
  `application_sdk.errors` API and already match.  An app-local helper is
  fine *if* it is named to the convention — one called `clean_message`
  redacts correctly and still leaves the finding standing.
- **Only the log call's own arguments are inspected.**  A sanitizer used
  elsewhere in the handler does not exempt an unrelated log call.
- **The level counts.**  For E004 the sanitizer exemption, like the
  `exc_info` one, applies only to `warning`, `error` and `critical`: a
  `debug` call through a sanitizer does not clear E004 on its own.  F005
  forbids `warning`/`warn` inside `preflight_check`; `error`/`critical` are
  E004-clearing but duplicate the gate's outcome row, so preflight still
  uses the typed-return shape below, not its log line — keep that line at
  `debug` with the sanitizer, as `atlan-mysql-app app/handler.py` does.

Never propose an inline `ignore[...]` here.  A suppression records that the
rule was skipped; the sanitized form records that the credential was handled.
Only the second is true, and only the second survives review.

**Mechanical rules** (`autofixable = true`) — produce a `"fix"` outcome with
`classification = "mechanical"`:

- **E005 ExceptBlockMissingExcInfo** — add `exc_info=True` to the log call
  inside the except block.  The edit is always a single keyword argument
  addition.  Example: `logger.warning("msg")` → `logger.warning("msg",
  exc_info=True)`.

  **Carve-out — pre-raise logs.** If the call is within the three
  statements before a `raise` in the same block, it is also an L009 site
  (logging area).  Do not add `exc_info=True` and do not switch to the
  sanitized `safe_traceback` form: that clears E005 and L004 but leaves L009,
  and the later L009 fix deletes the enriched line.  Apply L009's resolution
  in the same edit, in this order: delete the line when the exception has an
  upstream record (for anything raised out of an `@task` method on a v3 SDK
  worker, the SDK's `activity.ended` ERROR record; on a v2 app, confirm the
  caller logs it), which may leave a bare `raise` (never reduce the handler
  to one without that record); otherwise downgrade it to a sanitized DEBUG
  through locals (`cause = sanitize_cause_repr(exc)`,
  `trace = safe_traceback(exc)`, `logger.debug("...%s\n%s", cause, trace)`)
  and keep the raise.  That one edit
  clears E005, L004 and L009 together.  The logging area's "Pre-raise
  carve-out" has the full resolution.

  **Contraindication — redaction boundaries.** Never add `exc_info=True` when
  the call's arguments flow through a redaction helper (`redact*`, `sanitiz*`,
  `safe_traceback`, `scrub_secret*`, `mask_secret*`) or an adjacent comment
  says traceback capture is omitted on purpose: driver/API exception text can
  embed credentials (JDBC URLs, Authorization headers, OAuth bodies), and the
  separately-serialized traceback bypasses the redaction the code performs.
  The checker exempts sanitizer-bearing calls; if a finding still appears at a
  commented boundary, route it to residue — do not "fix" it.

- **E016 MissingExceptionChaining** — add `from exc` (or `from e`, matching
  the existing except clause variable) to the bare `raise X(...)` inside the
  except block.  Example: `raise ValueError(msg)` → `raise ValueError(msg)
  from exc`.

**Judgment rules** (`autofixable = true` — the lane applies the prescription;
`classification = "judgment"` — every result is routed to residue for audit,
because each site needs a call on what to log or raise) — produce a `"fix"`
outcome mirroring the error-handling shape in the reference app named by
`finding.canonical_reference`:

- **E002 TypedExceptPass** — the `except SomeError: pass` swallows the
  exception silently.  Propose replacing `pass` with a log call:
  `logger.warning("Ignoring %s: %s", type(e).__name__, e, exc_info=True)`,
  where `e` is the except clause variable (or `exc` if bare).  If the
  surrounding context suggests a best-effort probe (e.g. feature detection at
  import time), note this in the residue as a suppression candidate.

- **E001 BareExceptPass** — same treatment as E002 but bare `except:`.
  Propose adding a typed `Exception` clause and a log call.

- **E003 BroadContextlibSuppress** — `contextlib.suppress(Exception)` (or
  `BaseException`) hides every failure in the block with no record.  Narrow
  the suppression to the one condition actually tolerated
  (`suppress(FileNotFoundError)`), or replace it with `try/except <Type>` and
  a log line when the swallow needs evidence.  No reference app uses
  `suppress`; `atlan-metabase-app app/residuals.py`'s
  `record_residual_failure` shows the shape a deliberately tolerated failure
  takes — narrowed, and written to `residual/failures.jsonl` so a reviewer can
  find it.  A narrow `suppress` on a cleanup path is already acceptable and
  the checker does not flag it.

- **E004 BroadExceptClause** — `except Exception` / `except BaseException`
  whose body neither re-raises, returns the failure as typed data, nor logs the
  trace.

  **First, check whether the body already clears the rule** — see the list of
  already-clearing shapes below.  Adding a log call to one of them is a wrong
  edit, not a redundant one.

  **Otherwise default to the additive edit: log with `exc_info=True`.**  Add it to the
  log call already in the block, or add
  `logger.error("<what failed>: %s", exc, exc_info=True)` where there is
  none.  This clears the finding (the checker passes a body containing
  `logger.exception()`, or `warning`/`error`/`critical` with `exc_info=True`)
  and it **changes no control flow** — which is the whole reason it is the
  default for an unattended lane.  Mirror `atlan-openapi-app
  app/api_client.py`'s `_parse_zip`, which catches `Exception` per archive
  member and logs with `exc_info=True`.

  **Do not narrow the clause as the automatic fix.**  Narrowing `except
  Exception` to a specific type also clears the rule, and it is usually the
  better end state, but it silently changes *which exceptions propagate*: get
  the type wrong and an error that used to be handled now escapes, on a path
  the orthogonal gate very likely does not cover.  Propose a narrowing only as
  a residue suggestion, naming the type you inferred and the call inside the
  `try` you inferred it from — never applied in the same unit as the log edit.

  **Check the credential boundary before adding `exc_info=True`** — see the
  contraindication at the top of this section.  At a connect/auth/token site
  the fix is `sanitize_cause_repr(exc)` rather than the traceback, which
  clears E004 by the same sanitizer rule and leaks nothing.

  **Already-clearing shapes — do not "fix" any of these:**

  - a body that re-raises on every path with the trace preserved (bare
    `raise`, or `raise X(...) from e`);
  - a `raise X(...) from None` whose raised error carries the caught exception
    through a redaction helper;
  - a `warning`/`error`/`critical` log call whose arguments already flow
    through a redaction helper — a sanitized `debug`/`info` call does **not**
    clear E004, so raising it is a real edit, not a no-op (see *The level
    counts* above);
  - a body whose every exit path hands the caught exception back as **typed
    data** — `return PreflightCheck(passed=False,
    error=SourceUnavailableError(cause=exc).to_failure_details())`, or a row
    staged in a local that the enclosing function returns below the `try`.

  That last shape is the one to watch in an unattended lane.  It is the
  last-resort arm of a preflight probe, which deliberately fails *closed* with
  a typed verdict rather than letting an unexpected error crash the gate, and
  it is already clear of E004 — the failure leaves the frame as data and the
  SDK gate re-emits it at ERROR as the single `Preflight gate outcome` row.
  **Never add a `warning`/`error` log to it.**  Inside a `preflight_check`
  override that edit trades an E004 you did not have for an F005 you did not
  have either: F005 forbids `warning` there because the customer's log view
  filters at ERROR (FND-901), and an `error` line duplicates the gate's own
  outcome row.  If such a site still reports E004, the exception is *not*
  leaving the frame typed — most often it is handed off raw
  (`failed_check(name, exc, start)` proves nothing about its type under a
  broad catch) or one arm returns `None`.  Fix that, not the log.

  Recognition of the typed row is by naming convention: a call to a
  *capitalised* type that receives the caught binding.  Two shapes that do
  carry the failure still read as untyped, so restructure rather than
  suppress:

  - **A lowercase helper builds the row** —
    `return [_check(name, error=classify_driver_error(exc))]`.  Build it
    inline instead:
    `return PreflightCheck(name=..., passed=False, error=classify_driver_error(exc).to_failure_details())`.
  - **The arm sits in a loop body** and `append`s its row to a list returned
    after the loop — the arm falls through to the next iteration, so the row
    is not provably returned.  Move one probe into a helper that returns its
    own `PreflightCheck`, and have the loop append the helper's result.

  `atlan-mysql-app app/handler.py`'s `_check_connectivity` (one of
  `preflight_check`'s probes) is the reference: it converts the caught
  exception into a typed `PreflightCheck` row and returns it, with no
  suppression and no log above DEBUG.

  **Best-effort cleanup reached from `preflight_check` is the other trap.**  A
  helper such as `try: await client.aclose() except Exception:
  logger.debug(...)` that the gate calls has no verdict to return, and the
  levels close in on it: DEBUG (even through a redaction helper) does not
  clear E004, and WARNING trips F005 because the helper runs inside the gate.
  The recommended log that satisfies both is
  `logger.error("<what failed>: %s", safe_traceback(exc))` — or
  `sanitize_cause_repr(exc)`; `logger.critical` also clears both — or return the failure as typed data if the
  caller can carry it.  Do not narrow the clause automatically; see above.

- **E007 ErrorToReturnValue** — the `except` block returns a sentinel
  (`None`, `{}`, `[]`, `False`) with no logging before the `return`, so the
  caller sees a wrong result and no trace.  Add a
  `logger.warning(..., exc_info=True)` **before** the return — under a
  narrow catch, or before a non-empty return, the checker clears on any
  logging call preceding it — or raise a domain error when the caller cannot
  act on an empty value.  `atlan-openapi-app app/api_client.py`'s
  `redact_url` is the shape: it catches the narrow `ValueError` from
  `urlsplit`, logs it through `sanitize_cause_repr`, and only then returns its
  non-empty placeholder — so the placeholder stays the function's contract and
  the failure still leaves a trace.  It is **not** licence to log before an
  empty return under a broad catch; see the logged-sentinel arm below.  The
  log call you add is subject
  to the credential-boundary contraindication above — at an auth or connect
  site, format the exception through `sanitize_cause_repr` and omit
  `exc_info=True`.

  **Logged-sentinel arm — a log does not fix it:** under a broad catch
  (`except Exception` / `BaseException` / bare `except:`), a handler that
  already logs and then returns an empty sentinel (`None`, `[]`, `{}`, `()`,
  `""`, `dict()` …) still reports E007, with the message "logs and then returns
  an empty sentinel".  Adding, moving or upgrading the log is a wrong edit.
  Either raise a domain error (`raise XError(...) from exc`) so the caller gets
  an error code, or return the failure as typed data (the already-clearing
  shape below).  The one already-clearing variant is a per-item `return None`
  after the handler calls the imported `residuals.record_residual_failure`
  with an output path and a category (atlan-metabase-app
  `app/extracts/questions.py`): the failure is recorded as durable data, not
  just logged.  An empty aggregate (`{}`, `[]`) after that call still fires.
  If the empty value genuinely is the function's documented contract — for
  example an optional cache whose misses fall back to another lookup — a
  justified `# conformance: ignore[E007] <which contract>` is the end state, as
  described in the rule's terminal state.  Put that suppression in the app;
  do not teach the detector to prove the contract.  This arm skips narrow
  catches, a bare `return`, a `return None` in a `-> None` function, and
  wholesale-deprecated modules (a module-level `DeprecationWarning` in the file
  or an enclosing package's `__init__.py`), which are being removed rather
  than fixed.

  **Per-object skips — raising is a behaviour change:** when the handler sits
  in a batch or per-item loop (one object's fetch or parse), "raise a domain
  error" changes the loop's contract: one bad item then fails the whole run.
  It is never the default fix there.  Keep the skip and make the failure
  durable with the per-item `record_residual_failure` shape above, or raise
  the typed error and catch it at the loop.  Existing tests that assert the
  skip (`is None`, `== {}`) must keep passing unchanged.  The residual shape
  only clears a per-item `return None`; when the per-item value is an empty
  aggregate (`{}`, `[]`) and no clearing shape keeps the tested skip, the end
  state is a justified `# conformance: ignore[E007] <per-item skip contract>`
  or a judgment residue — never a raise.  E020 carries the same distinction
  for HTTP response guards.

  **Already-clearing shape — do not "fix" it:** a `return` that hands the
  caught exception back as **typed data** is not flagged.  E007 uses the same
  typed-failure predicate as E004's already-clearing list above, so the two
  rules agree:
  `return self._failed("authentication", started, AuthRejectedError(cause=exc))`,
  `return None, self._failed("credentials", started, CredentialsUnusableError(cause=exc))`,
  or, under a narrow catch (`except AuthRejectedError as exc:`), a helper
  that receives the binding directly: `return self._failed(name, started, exc)`.
  These are the arms of a preflight probe.  **Never add a log to them**:
  inside a `preflight_check` override a `warning`/`error` trades the E007 for
  an F005, as described under E004.  If such an arm still reports E007, the
  exception is not leaving the frame typed.  Usually it is handed raw to a
  helper under a broad catch, or the return stringifies it.  Wrap it in the
  domain error (`XError(cause=exc)`) rather than logging.  A bare sentinel
  and a stringified exception (`str(exc)`, `repr(exc)`, an f-string or
  `.format(exc)`) still fire.  A string is the failure laundered into a plain
  value.

- **E008 ImportErrorWithoutLogging** — `except ImportError` that hides a
  missing or broken dependency, so it reads as a normal skip.  When the handler
  takes a fallback or returns without surfacing the failure, bind the exception
  and carry its text into a log line, as `application_sdk/clients/ssl_utils.py`'s
  `_get_default_ca_bundle_path` logs its certifi fallback (no reference app has
  a scanned `except ImportError`; E008 skips `tests/`).  A cause-preserving
  re-raise on every path, including wrapping the ImportError in a typed domain
  error (`raise MissingExtraError(cause=exc) from exc`), already surfaces the
  failure and does not need a duplicate log.  A correct fallback still needs a
  trace; correctness alone does not make the missing-import event visible.

- **E009 ExceptBlockOnlyAssigns** — the `except` block only assigns a variable
  (a flag, a default) and logs nothing, so the failure sets state invisibly.
  Add `logger.warning("<what failed>: %s", exc, exc_info=True)` before the
  assignment; the checker clears on any logging call in the block.  Mirror
  `atlan-metabase-app app/credentials.py`'s `build_credential_ref`, which
  binds the routing error, logs it, then takes the inline-credentials path.
  A bound name that is never read afterwards is the tell that the handler is
  a placeholder — say so in the edit description rather than inventing a use
  for it.  These handlers sit on credential paths more often than most, so
  apply the contraindication above: prefer `sanitize_cause_repr(exc)` to
  `exc_info=True` wherever the bound exception came from an auth call.

- **E010 AsyncioGatherExceptionsUnexamined** — `asyncio.gather(...,
  return_exceptions=True)` returns exception *instances as values*, and the
  result list is never inspected, so every failure is discarded.  Iterate the
  results and handle the exceptions explicitly
  (`for r in results: if isinstance(r, Exception): logger.error(..., exc_info=r)`),
  raising or recording per item as the call site requires.  No reference app
  calls `gather(return_exceptions=True)`; per-item failure is decided at the
  item, as in `atlan-metabase-app app/extracts/dashboards.py`, where
  `fetch_dashboards_details` fetches one dashboard at a time and
  `fetch_dashboard_details` logs each failed fetch and records it as a
  residual.  Where the app
  genuinely needs concurrency the app-facing seam is
  `application_sdk/execution/heartbeat.py` — `run_in_thread` /
  `run_fault_isolated` / `run_best_effort`, which surface per-unit failures.
  Do **not** propose `_runtime.offload`: that is SDK-internal and importing it
  from an app is exactly what P005 flags.

- **E011 LoggingFilterUnsafeBody** — a `logging.Filter.filter()` body is not
  wrapped in `try/except`.  `Logger.handle()` calls it with no protection —
  unlike handler errors, filter exceptions are **not** caught by
  `handleError()` — so a raise here propagates into the caller that was merely
  logging.  Wrap the whole body and fail open (return `True` on error, so a
  broken filter never silently drops records).  Better, where the filter is an
  app's own: delete it and use `get_logger`, since filtering and
  Temporal-context enrichment belong to
  `application_sdk/observability/logger_adaptor.py` and redaction to
  `application_sdk/errors/base.py` (`sanitize_cause_repr` /
  `safe_traceback`).  `atlan-mysql-app
  app/client.py` shows an app's entire logging setup — one import, one
  module-level logger.

- **E012 UntypedBuiltinRaise** — the code raises a bare `ValueError` /
  `RuntimeError` / `KeyError`, which reaches the Automation Engine as an
  opaque string with no category, code, audience or retryable field.  Raise a
  typed leaf instead.  Pick the SDK category from what actually happened —
  `InvalidInputError` for bad caller input, `AuthError` for credentials,
  `PreconditionError` for an unmet precondition, `InternalError` otherwise —
  and use the app's own subclass of it, creating one in the app's errors
  module when none fits.  Mirror `atlan-mysql-app app/failures.py`: eleven
  leaves, each subclassing an SDK category and owning a `code`.  Preserve the
  cause with `from exc` when raising inside an `except` (E016).  The
  subclass-with-a-`code` shape matters: raising the bare category leaf trips
  E018.

  Leave these raises as builtins; the rule does not report them, and a
  typed error would break the caller:

  - Inside a Pydantic validator (`@field_validator`, `@validator`), where
    Pydantic needs a `ValueError` to build its `ValidationError`.  In
    `@model_validator` / `@root_validator` only `ValueError` is left alone;
    a `TypeError` or `RuntimeError` there escapes unconverted, so convert
    it as above.
  - `KeyError` from `__getitem__` / `__delitem__` / `__missing__` defined
    in a class body (`Mapping.get` and `in` catch it).  A different
    builtin in the same method, such as `ValueError` in `__getitem__`,
    still reports and is converted as above.  The rule never reports
    `IndexError`, `AttributeError` or `StopIteration`.
  - Files under the repo-root `tools/` or `contract/` directories (build
    and dev tooling).  `app/tools/` is app code and still reports.

- **E014 ExceptLoopControlSwallow** — an `except` block inside a loop whose
  body is only `continue` / `break` / `pass`, with no logging, so a shrinking
  result set has no explanation.  Explain a tolerated skip before the
  loop-control statement.  For an expected format/probe mismatch (for example,
  trying candidate datetime formats), log a concise message at DEBUG without
  `exc_info=True`, then continue.  Mirror `atlan-metabase-app app/utils.py:46`,
  which logs the input and candidate format at DEBUG for a `ValueError` mismatch.
  Reserve WARNING/ERROR with `exc_info=True` for genuine unexpected per-item
  failures (or re-raise); `app/lineage/qi_reader.py`'s `iter_qi_records` shows
  traceback logging for an unparseable QI line.

- **E015 ExceptionTextInErrorMessage** — a typed error is raised with the
  caught exception interpolated into `message=` (`f"...{exc}"`, `str(exc)`,
  `repr(exc)`, or concatenation), which puts unsanitised driver/API text —
  potentially credentials in a JDBC URL or an `Authorization` header — into an
  operator-facing field.  Give `message=` a **fixed** operator-facing string
  and pass the original through the cause instead: `cause=e` (or `from e`), so
  the detail reaches the log and the wire envelope stays clean.  Mirror
  `atlan-mysql-app app/client.py`'s `get_iam_role_token`, which raises
  `IamTokenGenerationError` with a fixed message and `cause=e`.

- **E017 SecretNamedEvidenceKey** — BLOCK.  An error is constructed with an
  evidence kwarg whose name ends in `_secret`, `_password` or `_token`;
  `application_sdk/errors/wire.py` rejects it **at runtime**, so this is a
  live crash in the failure path, not a style point.  Remove the kwarg, or
  rename it to describe the failure rather than the credential
  (`token_source`, `auth_method`) — and never simply rename the key while
  still passing the secret value.  Mirror `atlan-openapi-app
  app/connector.py`'s `download_cloud_spec`, whose evidence is
  `service` / `retryable` / `suggested_action`.

- **E018 BareParentLeafRaise** — an `application_sdk.errors` parent leaf is
  raised directly (`InternalError(...)`, `InvalidInputError(...)`), so every
  distinct failure in that category collapses into one bucket on the
  dashboard.  Raise a connector-specific subclass that overrides `code`,
  adding it to the app's errors module when it does not exist.  Mirror
  `atlan-openapi-app app/errors.py`, where each connector-specific subclass
  has its own `code` and every raise site in `app/connector.py` uses one,
  apart from the sanctioned `InternalError(classification_pending=True)`
  placeholder in `run`.  Interacts with P003: the subclass's `code` must start
  with the parent leaf's category prefix, so read that rule before choosing
  the string.

- **E019 ExceptionTextInContractField** — the same leak as E015, but into a
  returned contract rather than a raise: inside `except … as exc`, a response
  or output contract (`AuthOutput`, `PreflightCheck`, …) is built with the
  exception interpolated into `message=`, a field a caller renders.  Classify
  the exception into the app's typed `AppError` (reuse the classifier the
  preflight checks already use), then return
  `error=err.to_failure_details()` on the contract — the statically-typed form
  of `AuthOutput.error` / `PreflightCheck.error`; a failed result renders the
  error's message, so no separate `message=` is needed.  The user keeps the
  reason, as authored text.
  Do not default to a fixed string like `"Authentication failed"`: it clears
  the rule and discards the reason.  If no class covers the failure (for
  example bad credentials), add one; do not fall back to a catch-all
  "unreachable" class.  Mirror `atlan-mysql-app app/handler.py`'s
  `preflight_check` probes, which return the typed error on the check.

- **E020 HttpFailureToEmptyReturn** — a checked HTTP failure (a test on
  `is_success` / `ok` / `status_code`) returns an empty or `None` sentinel, so
  a failed fetch publishes as an empty success.  Raise a typed error instead
  (see E012 for choosing the category), which is the default edit.  Where the
  empty return is deliberate, meaning one failure must not abort the crawl,
  keep the raise and catch the typed error in the tolerating function.  The
  handler logs with `exc_info=True`, records a residual and returns the empty
  sentinel, and the run declares the gap as `PARTIAL_SUCCESS`.
  `atlan-metabase-app app/extracts/responses.py` (`json_or_raise`) and
  `app/extracts/databases.py` show the shape.  A site that already records
  and declares its gap under an `ignore[E020]` is converted to that typed
  catch.  Never give it a raise-only edit, which deletes the degradation.

  **Raise versus skip — read before editing a per-object fetch.**  Raise the
  typed error for a failure of the whole request, and for transient outcomes
  (no response, 408, 429, 5xx).  A permanent per-object rejection inside a
  batch or iteration is a **skip**, not a failure: when one object's detail
  fetch (a table, routine or dataset looked up inside a loop) answers 400, 404
  or 410 because the object was dropped between listing and fetch, or is
  invalid, log a WARNING with the status and the source's error *reason*
  (never the response body), return the empty value and let the batch
  continue.  Converting a per-object skip into a raise is a behaviour change:
  one bad object then fails the whole extraction, which is how a lane fix
  turned a per-object skip into a run-wide production failure.  It is never
  the default fix, and existing tests that assert the skip (`== {}`) must keep
  passing unchanged; if they fail, the edit is wrong, not the test.  The
  compliant skip is already exempt, so no suppression is needed:

  ```python
  if not response.is_success:
      if response.status_code in (400, 404):
          logger.warning("Skipping %s: HTTP %s (%s)", name, response.status_code, reason)
          return {}
      raise SourceUnavailableError(...)  # transient / whole-request failure
  ```

  The exemption needs the empty return directly under an `if` that pins
  `<response>.status_code` to codes from {400, 404, 410} (`in (...)` or
  `==`, `and`-ed terms allowed) on the same response the failure test is
  about, and a warning/error log earlier in that same body whose message says
  the object is skipped ("Skipping …") and that passes the status itself as an
  argument (or an f-string field) — a status inside another expression
  (`response.status_code == 404`) does not count.  The "skip" wording is the
  explicit per-object contract: a status code alone cannot tell a per-object
  fetch from a whole-request one.  A local alias counts when
  its last binding among the guard's earlier siblings is
  `status = response.status_code` (or `... if response is not None else
  None`) and it is not rebound before the log; a named code constant
  (`in PERMANENT_CODES`) is not followed.  A
  site that carries an `ignore[E020]` on such a skip can drop the directive.
  So when an E020
  fires on a per-object fetch, split the branch into that shape: the
  permanent codes skip with the log, everything else raises.  A bare
  `if not response.is_success: return {}`, a transient or missing-response
  empty return, a skip with no log (or a debug/info log, or one without the
  status), an `or`-ed or range guard, and 401/407 still report.  A 403 skip
  also still reports; whether a permission denial should skip or raise is a
  site-level judgment this prescription does not make — leave the site's
  existing 403 behaviour as it is.

- **E021 DependencyUnavailableOverriddenToUser** — an error class whose
  category is DEPENDENCY_UNAVAILABLE overrides `audience` to `USER`, so it says
  "the customer's own system is down" under a category that means "an Atlan
  platform service is down".  Re-base the class on `SourceUnavailableError`
  (keep the class name and evidence fields), rename its `code` from
  `DEPENDENCY_UNAVAILABLE_*` to `SOURCE_UNAVAILABLE_*`, and delete the
  `audience` line, which the new base already sets.  Rename any tests that
  assert the old code or category in the same edit.  This changes gate
  behaviour for an app with a hard preflight gate: the failure now blocks on
  the gate's final attempt, so the PR title and body must say so.  Mirror
  `atlan-mysql-app app/failures.py`'s `SourceRestartingError`.  If the class
  really covers an Atlan-side service, the override is the bug: delete the
  `audience` line and keep the category.

- **E013 LegacyAtlanErrorRaise** — the code raises a deprecated `AtlanError`
  subclass.  Consult the `/typed-failures` prescription: propose replacing
  with the appropriate `AppError` subclass from
  `application_sdk.common.error_codes`.  Choose the subclass by matching the
  raise site's semantic category (connection, permission, not-found, etc.) to
  the `AppError` hierarchy.  Classification is always `"judgment"` — the
  mapping requires understanding the call-site intent.

- **E006 BareExceptWithBody** — bare `except:` with a non-empty body.
  Propose narrowing to `except Exception as exc:` and adding
  `exc_info=True` to any existing log calls in the body.

- **All other E-series rules (E003, E004, E007–E012, E014, E015, E017, E018, E020, E021)** — produce
  `classification = "judgment"` and a best-effort fix guided by the `hint` and
  `message`.  (E020: replace the empty/None return on
  a checked HTTP-failure branch with a raised typed `AppError` so the failure
  propagates instead of publishing an empty success.  Where the site degrades
  deliberately, catch that typed error at the tolerating function rather
  than suppressing.  A permanent per-object 400/404/410 inside a batch is a
  logged skip, never a raise; see the E020 entry above.)

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose a suppression instead of a fix if it judges the pattern a legitimate
exception for this specific site (e.g. an E004 broad-except at a genuine
top-level worker loop boundary).  The suppression edit is an inline directive
inserted as a comment on the line above the violation:

```
# conformance: ignore[E004] <concise justification, 8–40 words>
```

The justification must describe _why_ the pattern is acceptable here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit regardless.
