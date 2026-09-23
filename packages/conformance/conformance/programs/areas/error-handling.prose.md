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

  `atlan-mysql-app app/handler.py`'s `preflight_check` is the reference: its
  probes convert the caught exception into a typed `PreflightCheck` row and
  return it, with no suppression and no log above DEBUG.

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
  `logger.warning(..., exc_info=True)` **before** the return — the checker
  clears on any logging call preceding it — or raise a domain error when the
  caller cannot act on an empty value.  `atlan-metabase-app
  app/extracts/databases.py`'s `fetch_databases_summaries` logs the HTTP
  status and records a residual before returning `[]`.  Where the sentinel
  really is the contract, `atlan-openapi-app app/api_client.py`'s `redact_url`
  carries an inline `ignore[E007]` saying so.  The log call you add is subject
  to the credential-boundary contraindication above — at an auth or connect
  site, format the exception through `sanitize_cause_repr` and omit
  `exc_info=True`.

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

- **E008 ImportErrorWithoutLogging** — `except ImportError` with no logging,
  so a missing or broken dependency reads as a normal skip.  Bind the
  exception and carry its text into whatever the block does next: a log line
  for a runtime guard, or the skip reason for a test guard, as in
  `atlan-openapi-app tests/e2e/test_connection_create.py`, which binds
  `except ImportError as _exc` and puts the text in the `pytest.skip` reason.
  Legitimate optional-dependency guards still need the trace — the fallback
  being correct is not the same as the failure being invisible.

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
  item, as in `atlan-metabase-app app/extracts/collections.py`.  Where the app
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
  app's own: delete it and use `get_logger`, since filtering, redaction and
  Temporal-context enrichment belong to
  `application_sdk/observability/logger_adaptor.py`.  `atlan-mysql-app
  app/client.py` shows an app's entire logging setup — one import, one
  module-level logger.

- **E012 UntypedBuiltinRaise** — the code raises a bare `ValueError` /
  `RuntimeError` / `KeyError`, which reaches the Automation Engine as an
  opaque string with no category, code, audience or retryable field.  Raise a
  typed leaf instead.  Pick the SDK category from what actually happened —
  `InvalidInputError` for bad caller input, `AuthError` for credentials,
  `PreconditionError` for an unmet precondition, `InternalError` otherwise —
  and use the app's own subclass of it, creating one in the app's errors
  module when none fits.  Mirror `atlan-mysql-app app/failures.py`: six
  leaves, each subclassing an SDK category and owning a `code`.  Preserve the
  cause with `from exc` when raising inside an `except` (E016).  The
  subclass-with-a-`code` shape matters: raising the bare category leaf trips
  E018.

- **E014 ExceptLoopControlSwallow** — an `except` block inside a loop whose
  body is only `continue` / `break` / `pass`, with no logging, so a shrinking
  result set has no explanation.  Add a log line before the loop-control
  statement — DEBUG when skipping a bad item is routine, WARNING when it is
  not — with `exc_info=True`.  Mirror `atlan-metabase-app
  app/lineage/qi_reader.py`'s `iter_qi_records`, which skips an unparseable
  line only after logging it with `exc_info=True`.

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
  `atlan-openapi-app app/errors.py`, where every raise site uses a subclass
  with its own `code`.  Interacts with P003: the subclass's `code` must start
  with the parent leaf's category prefix, so read that rule before choosing
  the string.

- **E019 ExceptionTextInContractField** — the same leak as E015, but into a
  returned contract rather than a raise: inside `except … as exc`, a response
  or output contract (`AuthOutput`, `PreflightCheck`, …) is built with the
  exception interpolated into `message=`, a field a caller renders.  Classify
  the exception into the app's typed `AppError` (reuse the classifier the
  preflight checks already use), then return `message=err.message` and
  `error=err` on the contract.  The user keeps the reason, as authored text.
  Do not default to a fixed string like `"Authentication failed"`: it clears
  the rule and discards the reason.  If no class covers the failure (for
  example bad credentials), add one; do not fall back to a catch-all
  "unreachable" class.  Mirror `atlan-mysql-app app/handler.py`'s
  `preflight_check` probes, which return the typed error on the check.

- **E020 HttpFailureToEmptyReturn** — a checked HTTP failure (a test on
  `is_success` / `ok` / `status_code`) returns an empty or `None` sentinel, so
  a failed fetch publishes as an empty success.  Raise a typed error instead
  (see E012 for choosing the category), which is the default edit.  Where the
  empty return is deliberate, it needs an evidence trail *and* an inline
  `ignore[E020]` naming it — `atlan-metabase-app app/extracts/databases.py`
  has exactly that, pointing at the residual file that records the failure,
  and seven such justified sites exist across `app/extracts/`.  Without that
  trail the empty return has to raise.

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

- **All other E-series rules (E003, E004, E007–E012, E014, E015, E017, E018, E020)** — produce
  `classification = "judgment"` and a best-effort fix guided by the `hint` and
  `message`.  (E020: replace the empty/None return on
  a checked HTTP-failure branch with a raised typed `AppError` so the failure
  propagates instead of publishing an empty success.)

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
