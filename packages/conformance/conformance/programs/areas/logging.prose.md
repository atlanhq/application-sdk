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

### Continuity

Input-driven: re-render when any `*.py` file or `pyproject.toml` under `scope`
changes.  In the Claude Code skill path the skill caller drives re-invocation
on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "L"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "logging"`._

Consult the finding's `hint` and `message`, then read the actual source lines
around `finding.line` in `finding.file` before proposing a fix.

**Mechanical rules** (`autofixable = true`, `classification = "mechanical"`):

- **L004 ExceptBlockMissingExcInfoLog** — add `exc_info=True` as a keyword
  argument to the log call inside the except block.
  `logger.warning("msg")` → `logger.warning("msg", exc_info=True)`.
  If the call already has keyword arguments, append after them.

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

**All other L-series rules** (L003, L006, L008, L009, L010, L012, L014,
L016, L018, L019) — `autofixable = false`; produce `classification =
"judgment"` and a best-effort fix guided by the `hint` and `message` in the
finding.  L010 (CredentialInLogOutput) is a security finding; always route to
residue and never auto-apply.

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
