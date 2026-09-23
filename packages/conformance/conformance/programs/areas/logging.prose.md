---
kind: responsibility
name: logging-area
description: >
  Maintains the current L-series violation-set and drives remediation of
  logging conformance findings.  Mechanical fixes (method renames, kwarg
  additions, format-string rewrites) are applied automatically; judgment
  fixes (factory swaps, print replacements, complex rewrites) are proposed
  and routed to residue for human review.
---

### Maintains

The current set of unsuppressed L-series (logging) conformance findings in
the working tree, as reported by `suite.runner --series L`.

#### violations-logging

The fingerprint-set of all unsuppressed FAILING L-series results.  Extends to
include WARNING results in strict mode.

This facet's fingerprint moves when any L-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series L` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.
- `rule_ids` — optional list of exact rule IDs (propagated from the
  top-level entry). Forwarded verbatim into every runner invocation this
  area makes — the loop's detect calls and the suggest-only
  `detect-violations` calls alike — so a `--rule`-scoped run stays scoped
  here rather than silently widening to the whole series at this hop.

### Continuity

Input-driven: re-render when any `*.py` file or `pyproject.toml` under `scope`
changes.  In the Claude Code skill path the skill caller drives re-invocation
on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "L"
  rule_ids: rule_ids
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "logging"`._

Consult the finding's `hint` and `message`, then read the actual source lines
around `finding.line` in `finding.file` before proposing a fix.

#### Credential-boundary contraindication — read before adding `exc_info=True`

The raw exception from a database driver, an HTTP client or an auth call can
embed credentials: a JDBC URL carrying a password, an `Authorization` header
or HMAC, connection properties, an OAuth response body.  `exc_info=True`
serialises the traceback *separately*, so it bypasses whatever redaction the
message itself performs — adding it at such a site **creates a leak that was
not there before**.  A production security review over the fleet remediation
(FND-57) found exactly this shape in five connector repos.

Before adding `exc_info=True` (L004, and the L005/L017 rewrites that add it),
ask what the caught exception can carry.  If the `try` wraps a connect, an
authenticate, a token refresh, or any request whose URL, headers or body hold
a secret, **do not add it**.  Log through a redaction helper instead:

```python
from application_sdk.errors import redact_secrets, sanitize_cause_repr

logger.error("connect failed: %s", sanitize_cause_repr(exc))
```

**Keep the stack, redacted.** The one-line form above drops the traceback,
which is the defect L004/E005 exist to fix — a postmortem is back to
reproducing the failure against the customer's source. When the stack is
worth having (anything past a single, well-understood connect call), log
the redacted traceback alongside the redacted cause:

```python
from application_sdk.errors import safe_traceback, sanitize_cause_repr

logger.error("connect failed: %s\n%s", sanitize_cause_repr(exc), safe_traceback(exc))
```

`safe_traceback` formats the full chain (frames, `__cause__`, `__context__`)
and runs it through the same redaction as `redact_secrets`, so the frames
survive and the URL userinfo and secret query params do not. It is a
recognised sanitizer name, so the call clears the rule the same way.

**This clears the rule, and needs no suppression** — L004 accepts a log call
whose arguments flow through a sanitizer as a deliberate no-traceback
boundary (`suite/checks/_ast_common/_sanitizers.py`).  Recognition is **by
name**: the callable must contain `redact`, `sanitiz`, `scrub_secret`,
`mask_secret` or `safe_traceback`, or the argument must be a variable named
for redacted output (`safe_traceback`, `redacted`, `sanitized`, `masked`,
`scrubbed`).  The `application_sdk.errors` helpers above are public API and
already match; an app-local helper named `clean_message` redacts correctly
and still leaves the finding standing.  Only the log call's own arguments are
inspected, so a sanitizer used elsewhere in the handler does not exempt it.

Never propose an inline `ignore[...]` here: a suppression records that the
rule was skipped, the sanitized form records that the credential was handled.

**Mechanical rules** (`autofixable = true`, `classification = "mechanical"`):

- **L004 ExceptBlockMissingExcInfoLog** — add `exc_info=True` as a keyword
  argument to the log call inside the except block.
  `logger.warning("msg")` → `logger.warning("msg", exc_info=True)`.
  If the call already has keyword arguments, append after them.

  **Contraindication — redaction boundaries.** Never add `exc_info=True` when
  the call's arguments flow through a redaction helper (`redact*`, `sanitiz*`,
  `safe_traceback`, `scrub_secret*`, `mask_secret*`) or an adjacent comment
  says traceback capture is omitted on purpose: driver/API exception text can
  embed credentials (JDBC URLs, Authorization headers, OAuth bodies), and the
  separately-serialized traceback bypasses the redaction the code performs.
  The checker exempts sanitizer-bearing calls; if a finding still appears at a
  commented boundary, route it to residue — do not "fix" it.

- **L007 LoggerCriticalUsage** — rename `.critical(` to `.error(`.  If the
  call site is inside an except block and has no `exc_info` kwarg, also add
  `exc_info=True`.  Outside an except block, rename only.
  `logger.critical("msg")` → `logger.error("msg")`.

- **L015 DictConfigDisableExistingLoggers** — in the dict literal argument
  to `logging.config.dictConfig()`, set `"disable_existing_loggers": False`.
  If the key is absent, add `"disable_existing_loggers": False` as an entry.
  If it is present as `True`, change the value to `False`.

- **L017 LoggerExceptionUsage** — rename `.exception(` to `.error(` and add
  `exc_info=True` if not already present.
  `logger.exception("msg")` → `logger.error("msg", exc_info=True)`.
  If `exc_info=False` is already present, leave it (the caller intentionally
  suppressed the traceback); rename the method only.

- **L020 DeprecatedLoggingWarn** — simple rename: `.warn(` → `.warning(`.
  Also handles the module-level form: `logging.warn(` → `logging.warning(`.

**Judgment rules** (`classification = "judgment"`; route to residue):

- **L001 FStringInLogMessage** — rewrite the f-string as %-style.  Move each
  `{expr}` to a positional argument after the format string, replacing it with
  `%s` (use `%r` for repr, `%d` for clearly integer expressions).  For a
  single interpolation this is nearly mechanical; for complex nested
  expressions (conditional, method call, attribute chain) examine context and
  use `%s` with a clear string representation.  Never introduce `.format()`
  or concatenation as the replacement.

- **L002 NonCanonicalLoggerFactory** — swap to the canonical SDK adapter.
  Steps:
  1. Add `from application_sdk.observability.logger_adaptor import get_logger`
     to the import block if not present.
  2. Replace the non-canonical acquisition:
     - `logging.getLogger(name)` → `get_logger(name)`.
     - `structlog.get_logger(...)` → `get_logger(__name__)`.
     - `from loguru import logger` (direct import) → remove the import line;
       add `logger = get_logger(__name__)` at module level.
  3. Remove the now-unused `import logging` / `import structlog` line if no
     other usages remain in the file.
  Classification is always `"judgment"` — the import change affects the whole
  file and requires verifying that no other symbols from the removed import
  are still in use.

- **L005 PrintInProductionCode** — replace `print(...)` with a logger call.
  Choose level from context:
  - Output that describes an error or exception → `logger.error(...)`.
  - Output that looks diagnostic / verbose → `logger.debug(...)`.
  - Default / informational → `logger.info(...)`.
  Rewrite any f-string or concatenation in the print argument to %-style in
  the logger call (applying the L001/L011 transform).  Ensure `get_logger` is
  imported and a module-level `logger` is present; add them if missing.

- **L011 StringConcatenationInLog** — rewrite string concatenation as
  %-style.  Identify alternating segments: literal strings become the static
  parts of the format string; non-literal expressions each become a `%s`
  positional arg.  `logger.info("User " + name + " connected")` →
  `logger.info("User %s connected", name)`.  For `str(expr)` wrappers, drop
  the `str()` call and use `%s` (Python's `%` will call `str()` implicitly).

- **L013 StdlibArbitraryKwargs** — move non-allowlist kwargs into the message
  body.  Allowlist: `{exc_info, extra, stack_info, stacklevel}`.  For each
  non-allowlist kwarg `key=value`: append `key=%s` to the format string and
  move `value` to a positional argument after the existing args.  If the
  message is not already %-style, first rewrite it (applying L001/L011
  transform) before appending context.
  `logger.info("Connected", host=host, port=port)` →
  `logger.info("Connected host=%s port=%s", host, port)`.

- **L021 MissingLoggingLintRules** — add the missing rule IDs to
  `[tool.ruff.lint]` in `pyproject.toml`.  Prefer extending `extend-select`
  (not `select`) to avoid clobbering existing selections.  The missing rule
  IDs are listed in the finding message.  Add them as additional strings in
  the `extend-select` list; create the key if absent.  Example addition for
  G001, G003, G004, T201, LOG009:
  ```toml
  [tool.ruff.lint]
  extend-select = ["G001", "G003", "G004", "T201", "LOG009"]
  ```
  If a category prefix already covers some rules (e.g. `"G"` covers all
  G-rules), add only the genuinely missing individual IDs.

  **Never ADD the bare `"G"` category yourself.** `G` also enables `G201`,
  which demands `.exception(...)` over `.error(..., exc_info=True)` — the
  exact inverse of conformance L017 (LoggerExceptionUsage). Adding `"G"`
  makes ruff and the conformance suite contradict each other on every
  except-block log call. Always pin the five rules individually. If the repo
  already selects `"G"` on its own, leave it and route the conflict to
  residue for the owner.

  **Land it with or after the L001/L011 fixes, not before.** Enabling
  `G004`/`G003` while those findings are still open turns every one of
  them into a pre-commit `ruff` failure, so the L021 edit goes red on its own.
  Run `ruff check --select G003,G004 .` first; if it reports
  anything, fix those call sites in the same change (or order L021 after the
  L-series cleanup). L020 (`logger.warn()`) is ruff G010, which L021 does
  not require, so it does not belong in this pre-scan.

  **Scope `T201` away from tests and CLI scripts.** `print()` in `tests/`
  (pytest diagnostics, a manual `__main__` runner) and in `.github/**/*.py`
  scripts whose stdout is their output is intentional. Do not delete those
  prints; add a per-file ignore (the checker does not read
  `per-file-ignores`, so this still clears L021):
  ```toml
  [tool.ruff.lint.per-file-ignores]
  "tests/**/*.py" = ["T201"]
  ".github/**/*.py" = ["T201"]  # CLI scripts — print() is intentional stdout
  ```
  Run `ruff check --select T201 .` and cover only the directories it actually
  flags outside `app/`.

  **Expect formatter churn when the repo had no `[tool.ruff]` table.**
  Adding the first `[tool.ruff.*]` table makes ruff infer its target version
  from `requires-python`, and `ruff format` may then rewrite unrelated code
  (e.g. multi-context `with a, b:` into parenthesised form). That diff is a
  side effect of the config, not scope creep: keep it, and call it out in the
  PR description as formatting-only.

- **L003 ExtraKwargsWrongFramework** — the call passes `extra={...}`, so the
  context lands in an unindexed nested dict that aggregation queries cannot
  see.  Move every key into the `%`-style message body as a positional
  argument and delete the `extra=` kwarg:
  `logger.info("sync done", extra={"rows": n})` →
  `logger.info("sync done rows=%d", n)`.  No reference app passes `extra={}`
  at all — `atlan-metabase-app app/utils.py`'s `to_epoch_ms` logs
  `"Datetime %r did not match format %r", dt_str, fmt`.  Keep `exc_info`,
  `stack_info` and `stacklevel`; those are not context kwargs.

- **L006 InfoInTightLoop** — a `logger.info()` sits inside a loop, so the run
  emits one record per item and the lifecycle milestones drown.  Drop the
  per-item call to `logger.debug(...)` and, when the loop's outcome is worth
  an INFO, add **one** summary line after the loop
  (`logger.info("processed %d assets, %d skipped", total, skipped)`).  Mirror
  `atlan-metabase-app app/extracts/process.py`, where the per-dashboard skip
  inside `process_assets` logs at DEBUG.  Do not simply delete the call: the
  per-item record is still wanted at DEBUG.

- **L008 UnguardedExpensiveDebug** — an argument to `logger.debug()` is
  computed before the call, so it runs at every level.  Prefer making the
  argument cheap and letting `%`-style defer the interpolation
  (`logger.debug("payload %s", obj)` rather than
  `logger.debug("payload " + json.dumps(obj))`); where the expression is
  genuinely costly — a `json.dumps` of a large structure, a joined
  comprehension — wrap the call in `if logger.isEnabledFor(logging.DEBUG):`.
  `atlan-mysql-app app/client.py`'s `provide_token` shows the cheap form:
  `"IAM token refreshed for connection (length: %d)", len(token)`.

- **L009 WarnThenRaiseDuplication** — a bare `logger.warning()` or
  `logger.error()` statement sits within three statements of a `raise`, so the
  same failure is recorded twice: here, and again wherever the exception is
  finally handled.  The cost is inflated error counts on the dashboard, not a
  lost record.

  **Deleting the log line is the ideal end state and the wrong default.**  It
  is only correct if the exception really *is* recorded upstream — and these
  same repos carry open E002/E004/E007/E014 findings, which are precisely
  handlers that swallow without logging.  Delete into one of those and the
  failure becomes invisible, with every gate still green: no test covers it,
  and `no_new_findings` will not see it because the swallow was already there.

  So establish the caller first.  **If you can show the exception is logged
  upstream** — the handler that catches this type logs it with `exc_info=True`
  — delete the call; that is the shape `atlan-metabase-app app/connector.py`'s
  `transform_data` has, raising `MissingTypenameInputError` and
  `MissingOutputPathInputError` with no log line before either.  **If you
  cannot** (no handler in the repo, or the handler swallows), do not delete:
  **downgrade the level** to `logger.debug(...)`.  The rule matches only
  `warning` and `error`, so a DEBUG line clears the finding, stops inflating
  the error count, and keeps the local detail for whoever debugs it.

  Keep the line at its current level only when it carries context the
  exception genuinely cannot (a loop index, the URL being retried) — then the
  honest fix is to move that context into the exception and delete the log.
  Say in the edit description which of the three cases you found.

- **L010 CredentialInLogOutput** — BLOCK, and a security finding.  Log the
  credential's *name* or *type*, never its value: drop the offending argument
  or replace it with a non-secret descriptor
  (`logger.info("using credential %s", cred_name)`), and never a length, a
  prefix or a mask of the value itself.  `atlan-mysql-app app/client.py`'s
  `get_iam_role_token` records that AWS credentials were staged and names
  none of them.  **Always route to residue and never auto-apply**, whatever
  the mode: a human confirms every credential-shaped change.

- **L012 StdlibExtraReservedKeyCollision** — BLOCK.  A key in `extra={}`
  collides with a stdlib `LogRecord` attribute (`message`, `module`, `name`,
  `args`, …), which raises `KeyError` inside `Logger.makeRecord()` and crashes
  the caller — this is a live runtime break, not a style point.  Rename the
  key (`module` → `source_module`), or better, move the context into the
  `%`-style body as L003 prescribes and drop `extra=` entirely.  No reference
  app builds an `extra={}` dict; `application_sdk/observability/logger_adaptor.py`
  takes arguments positionally and injects the Temporal context itself.

- **L014 StructlogEventKwargOverwrite** — a structlog call passes `event=`,
  which *is* structlog's message key, so the domain value silently replaces
  the log message.  Rename the domain field (`event=` → `event_type=` or the
  name the payload actually means).  structlog is not a dependency of any
  reference app; `atlan-metabase-app app/api_types.py` uses the one canonical
  `get_logger` factory, which is the end state to migrate toward.

- **L016 BasicConfigNoopAfterFirstCall** — `logging.basicConfig()` is called
  more than once across the repo, and every call after the first is a silent
  no-op, so which configuration wins depends on import order.  Remove the
  app's calls: the SDK runtime owns handler configuration exactly once.
  `atlan-openapi-app app/run_dev.py` boots the runtime and calls it never.
  If a script genuinely runs outside the SDK runtime, consolidate to a single
  call in that entrypoint and say so in the edit description.

- **L018 KwargsInApplicationLogCalls** — arbitrary kwargs on an application
  log call land in an unindexed blob and never reach the message a reader
  greps.  Append each to the `%`-style template and pass the value
  positionally: `logger.info("connected", host=h, port=p)` →
  `logger.info("connected host=%s port=%d", h, p)`.  Mirror
  `atlan-metabase-app app/connector.py`'s `filter_data`
  (`"filter_data: include=%s, exclude=%s"`).  `exc_info`, `stack_info` and
  `stacklevel` are allowed and must be left alone.

- **L019 DiscardedBindResult** — `logger.bind(...)` returns a *new* bound
  logger and the result is thrown away, so the context is never attached.
  Assign it and use the bound logger for the calls that need the context
  (`log = logger.bind(run_id=rid)`).  Where the context is already injected by
  the SDK adaptor — workflow and run correlation always is — the honest fix is
  to delete the `bind()` call instead; no reference app calls it, and
  `atlan-metabase-app app/handler.py` uses the module-level logger directly.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose a suppression instead of a fix if it judges the pattern a legitimate
exception for this specific site (e.g. an L005 `print()` inside a
`__main__` guard that the checker could not statically detect as exempt, or an
L006 loop that is provably bounded to ≤10 items via a literal collection).
The suppression edit is an inline directive on the line above the violation:

```
# conformance: ignore[LXXX] <concise justification, 8–40 words>
```

The justification must describe *why* the pattern is acceptable here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit regardless.
