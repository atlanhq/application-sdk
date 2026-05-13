---
name: signal-over-noise
description: >
  Good investigations depend on good signals. Use this skill to improve the quality of
  observability in a Python codebase — surfacing failures that are currently hidden, and
  removing noise that buries the signals you need. Errors swallowed silently, stack traces
  discarded, log output that can't be queried, credentials leaking into logs, INFO-level
  chatter drowning out what matters: all of these slow down debugging and erode trust in
  your observability stack. Standardising log levels means DEBUG stays out of production,
  INFO marks genuine lifecycle events, and ERROR only fires when something actually failed
  — so when you filter to WARNING or above during an incident, you see exactly what you
  need and nothing you don't. Run this before an incident review, before onboarding a
  service, or any time investigations feel harder than they should.
argument-hint: "[--mode surface|tune] [--severity critical|high|medium|all] [path]"
mandatory_triggers:
  - "/signal-over-noise"
  - "audit observability"
  - "surface swallowed exceptions"
  - "tune logging noise"
optional_triggers:
  - "find hidden errors"
  - "clean up log levels"
  - "improve signal to noise"
  - "remove logging anti-patterns"
owner: connector-platform-team
last_updated: "2026-05-12"
staleness_days: 90
inputs:
  - target_path: "first positional arg — directory to audit (default: current working directory)"
  - mode: "surface | tune (default: surface)"
  - severity: "critical | high | medium | all (default: all)"
outputs:
  - remediation plan presented in plan mode for user review
  - edits applied to caught/silent exception sites (surface mode) or logging call sites (tune mode), left uncommitted
  - TODO comments at sites that require manual review
  - optional pyproject.toml ruff rule additions (tune mode, if approved)
gates:
  - "No edits applied before the user approves the remediation plan."
  - "Each phase runs pre-commit on changed files before handing back to the user."
  - "All changes left uncommitted — the user reviews and commits."
---

# Skill: `/signal-over-noise`

A two-phase Python observability audit that improves the ratio of meaningful signal to noise
in your error handling and logging. Each phase analyzes the codebase, presents a remediation
plan for your review, and — after approval — executes the fixes.

- **`--mode surface` (default):** uncovers suppressed exceptions that make failures invisible
- **`--mode tune`:** fixes logging anti-patterns that pollute or obscure your log stream

## Invocation

```
/signal-over-noise                                      # surface audit, current directory
/signal-over-noise packages/app-framework/src           # surface audit, specific path
/signal-over-noise --mode surface                       # same as default
/signal-over-noise --mode tune                          # logging quality audit
/signal-over-noise --mode tune --severity high          # HIGH + CRITICAL findings only
/signal-over-noise --mode surface packages/foo/src      # surface audit, specific path
```

**Argument parsing:**
- First positional token (not a flag): target path. Default: `.`
- `--mode surface|tune`: which phase to run. Default: `surface`
- `--severity <level>`: filter findings; levels: `critical`, `high`, `medium`, `all`. Default: `all`

---

## How each mode works

Both modes follow the same two-stage structure:

1. **Analysis** — grep the codebase, read context, classify findings, build a remediation plan.
   Enter plan mode and present the plan for user review. The user can iterate on the plan.

2. **Execution** — after the user approves the plan, apply the agreed fixes. Run pre-commit on
   changed files. Leave all changes uncommitted for the user to review before committing.

---

## Mode: surface

Finds every pattern where exceptions are caught and silently discarded, converting failures
into invisible state.

Pattern catalogue, severity criteria, fix templates, and linting rules are in:

`.claude/skills/signal-over-noise/references/error-recovery-patterns.md`

---

### Stage 1 — Analysis

#### Step 1.1 — Setup

1. Read `.claude/skills/signal-over-noise/references/error-recovery-patterns.md` in full.
2. Read `.claude/skills/signal-over-noise/references/typed-error-prescription.md` in full.
3. Resolve `TARGET_PATH` from arguments (default: current directory). Confirm it exists.
4. Set `SEVERITY_FILTER` from `--severity` (default: `all`).

#### Step 1.2 — Pattern Detection

Run all 13 grep searches in parallel against `TARGET_PATH` (`*.py` files only):

| # | Pattern ID | Grep |
|---|------------|------|
| 1 | P1 | `except\s*:\s*pass` |
| 2 | P2 | `except\s+\w.*:\s*pass` |
| 3 | P3 | `contextlib\.suppress\(` |
| 4 | P4 | `except\s+(Exception\|BaseException)\s*:` |
| 5 | P5 | `logger\.(warning\|error\|critical)\(` inside except blocks — collect hits for Step 1.3 |
| 6 | P6 | `^\s*except\s*:` |
| 7 | P7 | except block with `return` and no log — collect hits for Step 1.3 |
| 8 | P8 | `except\s+ImportError` |
| 9 | P9 | except block with only an assignment — collect hits for Step 1.3 |
| 10 | P10 | `return_exceptions=True` |
| 11 | P11 | `class\s+\w+.*logging\.Filter` or `def filter\(self` inside a Filter subclass |
| 12 | P12 | `raise\s+(ValueError\|RuntimeError\|Exception\|TypeError\|NotImplementedError\|OSError\|KeyError\|LookupError)\b` |
| 13 | P13 | `raise\s+(ClientError\|ApiError\|OrchestratorError\|WorkflowError\|IOError\|CommonError\|DocGenError\|ActivityError\|AtlanError)\b` |

For P5, P7, P9, P11, P12, P13: raw grep over-matches. Collect `file:line` hits and read context in Step 1.3.
P13 context note: `IOError` is also a Python builtin alias for `OSError` — confirm the hit imports from `application_sdk.common.error_codes` before classifying as legacy. Bare `AtlanError` raises (no constant) require litmus-test classification (§3 of `typed-error-prescription.md`).

Collect all hits as a flat list: `(file, line_number, pattern_id, raw_snippet)`.

#### Step 1.3 — Context Reading and Classification

For each hit, read 10–15 lines of surrounding context.

Classify each finding:
- **Severity:** CRITICAL, HIGH, MEDIUM, or LOW (from §Severity Classification in reference)
- **Legitimacy:** `genuine-bug` or `acceptable` (from §Legitimacy in reference)
- **Category:** `silent-swallow`, `missing-traceback`, `overly-broad-catch`,
  `error-to-return-value`, `suppress-operational`, `optional-import`,
  `asyncio-unexamined`, `untyped-raise`, `legacy-raise`
- **Typed-error prescription:** for every `genuine-bug` finding whose fix involves
  a `raise` (FT-1b, FT-3b, FT-5b, FT-8, FT-9), look up the prescribed leaf and
  code from `references/typed-error-prescription.md`. Record as `Prescribed:` on
  the remediation entry so the user sees the target class in the plan.

Apply `SEVERITY_FILTER`: discard findings below the threshold before proceeding.

P12 classification notes:
- Inside `__post_init__`, stdlib dataclass validators, or Pydantic `@field_validator` / `@validator` methods: check for a comment
  explaining the stdlib-interop need. If present → `acceptable`. If absent →
  flag as `genuine-bug` (MEDIUM — add the comment).
- Inside an `@task`-decorated activity body or `@activity.defn`-decorated function → escalate to CRITICAL.

P13: confirmed hits are `genuine-bug` (HIGH). After context reading (see over-matches note above), use the §5 migration table from
`typed-error-prescription.md` for the `Prescribed:` field. When no matching legacy constant is present, use §3 litmus tests to select a leaf directly.

#### Step 1.4 — Build Remediation Plan

For each `genuine-bug` finding, produce a remediation entry. Include `Prescribed:`
whenever the fix involves raising an exception:

```
File:     application_sdk/clients/sql.py:352
Severity: HIGH
Category: untyped-raise
Pattern:  P12
Current:
    raise ValueError("Engine is not initialized. Call load() first.")
Prescribed:
    EngineNotInitializedError(InternalError)  →  code=INTERNAL_ENGINE_NOT_INITIALIZED
    Subclass bakes message + component + invariant. Place in a sibling
    module (e.g. _<area>_errors.py); see typed-error-prescription.md §4.
Fix (FT-8):
    raise EngineNotInitializedError()
Auto-fixable: no  (leaf + subclass choice needs context — manual review)
Priority: P1
```

For existing-pattern (P1–P11) findings whose fix re-raises, also include
`Prescribed:` with the leaf selected from `typed-error-prescription.md` §4.

Priority mapping:
- **P0** — CRITICAL: fix immediately, block merges
- **P1** — HIGH: fix in current sprint
- **P2** — MEDIUM/LOW: fix when touching the file

#### Step 1.5 — Enter Plan Mode

Call `EnterPlanMode`.

Present the Surface Report as the plan:

```markdown
# Surface Report — <TARGET_PATH>

## Executive Summary
- Files scanned: N
- Total findings: N (N genuine bugs, N acceptable patterns)
- By severity: CRITICAL: N | HIGH: N | MEDIUM: N | LOW: N
- By category: silent-swallow: N | missing-traceback: N | ...
- Auto-fixable: N | Needs manual review: N

## Findings
| # | File | Line | Severity | Category | Pattern | Auto-fixable | Notes |
|---|------|------|----------|----------|---------|--------------|-------|

## Remediation Plan

### P0 — Fix Immediately (CRITICAL)
[entries]

### P1 — Fix This Sprint (HIGH)
[entries]

### P2 — Fix When Touching (MEDIUM/LOW)
[entries]

## Acceptable Patterns (no action required)
| File | Line | Pattern | Justification |
|------|------|---------|---------------|
```

Call `ExitPlanMode` to request user approval. Wait for approval before proceeding to Stage 2.

---

### Stage 2 — Execution (after plan approval)

Apply the remediation plan that was approved. Only fix what was listed in the approved plan.

#### Auto-fixable patterns — apply directly:
- **FT-1a:** Add logging to silent-swallow (log-and-continue, best-effort paths)
- **FT-2:** Add `exc_info=True` to existing log call in except block
- **FT-3a:** Add logging before error-to-return-value (when caller treats default as valid)
- **FT-4:** Add exception inspection after `asyncio.gather(..., return_exceptions=True)`
- **FT-5a:** Replace broad `contextlib.suppress(Exception)` with logged try/except (best-effort)

#### Manual review required — leave TODO comments:
- **FT-1b** (log + typed re-raise): leaf choice is contextual
- **FT-3b** (convert swallow to typed raise): default for error-to-return-value when caller trusts result
- **FT-5b** (typed re-raise for non-best-effort suppress): leaf choice is contextual
- **FT-6** (narrow broad catch): `# TODO(signal-over-noise): [P4] narrow this catch`
- **FT-7** (Filter exception safety): `# TODO(signal-over-noise): [P11] wrap filter body in try/except`
- **FT-8** (convert untyped builtin raise to typed `AppError`): leaf from `typed-error-prescription.md` §4. Define a subclass for each distinct failure mode in a sibling errors module (SDK: `application_sdk/<area>/errors.py` or `_<area>_errors.py`; app: `app/failures.py`). Bake `message` and evidence-field defaults when they're stable across raise sites; for one-off sites, the subclass may just override `code` and let the raise site pass `message`.
- **FT-9** (convert legacy `AtlanError` raise to typed `AppError`): leaf from `typed-error-prescription.md` §5. Same subclassing rule applies when the same error recurs.

TODO comment format for swallow/logging findings:
```python
# TODO(signal-over-noise): [P2] silent swallow — add logging. See references/error-recovery-patterns.md#P2
```

TODO comment format for untyped/legacy raise findings — include the prescribed leaf:
```python
# TODO(signal-over-noise): [P12] convert to typed AppError. Leaf: InternalError (wire code: INTERNAL_ENGINE_NOT_INITIALIZED). See typed-error-prescription.md §4
# TODO(signal-over-noise): [P13] legacy AtlanError — migrate to DependencyUnavailableError. See typed-error-prescription.md §5
```

#### After applying fixes:

Run pre-commit on every file that was changed:
```bash
uv run pre-commit run --files <file1> <file2> ...
```

If pre-commit is not available, skip this step (do not fail).

Leave all changes **uncommitted** — the user reviews before committing.

#### Finish

Report a brief summary: N files changed, N auto-fixes applied, N TODO comments added.

Then recommend the next step:

> **Next:** Run `/signal-over-noise --mode tune` to audit logging quality (f-strings, missing `exc_info`, log levels, credential leaks, and more).

---

## Mode: tune

Finds logging anti-patterns that pollute your log stream, obscure structured fields, or create
security risks.

Pattern catalogue, severity criteria, fix templates, and linting rules are in:

- `.claude/skills/signal-over-noise/references/logging-patterns.md` — detectable patterns (L1–L23)
- `.claude/skills/signal-over-noise/references/logging-level-guidelines.md` — level philosophy and framework awareness notes

---

### Stage 1 — Analysis

#### Step 2.1 — Setup and Discovery

1. Read both reference files listed above in full.
2. Resolve `TARGET_PATH` from arguments (default: current directory). Confirm it exists.
3. Set `SEVERITY_FILTER` from `--severity` (default: `all`).

**Discover the project's logging framework** — do not assume any specific one:

Grep for common logger factory patterns and count occurrences:
- `structlog\.get_logger\(`
- `logging\.getLogger\(`
- `loguru` (import or usage)
- `get_logger\(` (custom wrapper)

The factory with the highest count is `CANONICAL_FACTORY`.

Check `pyproject.toml` and `requirements.txt` / `uv.lock` for logging framework dependencies:
- `structlog` in deps → `LOGGER_FRAMEWORK = structlog`
- `loguru` in deps → `LOGGER_FRAMEWORK = loguru`
- Neither → `LOGGER_FRAMEWORK = stdlib`

Determine `SUPPORTS_KWARGS`:
- structlog or loguru: `SUPPORTS_KWARGS = true`
- stdlib: `SUPPORTS_KWARGS = false`

**Determine `FRAMEWORK_SPECIFIC_PATTERNS`** — which additional patterns (L13–L23) to run:
- stdlib: L13, L14, L16, L17, L20, L23
- structlog: L15, L19
- loguru: L19, L21, L22
- All frameworks: L18

**Discover logger variable names** — grep for assignments:
```
logger\s*=\s*
log\s*=\s*
_logger\s*=\s*
_log\s*=\s*
```

Collect distinct prefixes (e.g. `logger`, `log`, `_logger`). Build `LOGGER_VAR_PATTERN` by
joining them: `(logger|log|_logger|_log)\.` — use this in all Step 2.2 greps.

#### Step 2.2 — Pattern Detection

Run the universal patterns (L1–L12) plus `FRAMEWORK_SPECIFIC_PATTERNS` (L13–L23) in parallel
against `TARGET_PATH`. Use `LOGGER_VAR_PATTERN` in place of hardcoded `logger\.` in each pattern.

**Universal patterns (all frameworks):**

| Pattern | Grep | Type |
|---------|------|------|
| L1 f-string | `LOGGER_VAR_PATTERN\.\w+\(f["']` | mechanical |
| L2 inconsistent factory | Files not using `CANONICAL_FACTORY` | mechanical |
| L3 `extra={}` | `extra=\{` near a logger call | heuristic |
| L4 missing exc_info | multiline: `except` + log call without `exc_info` | heuristic |
| L5 print() | `^\s*print\(` (exclude test files, CLI scripts) | mechanical |
| L6 INFO in loop | `LOGGER_VAR_PATTERN\.info\(` inside for/while | heuristic |
| L7 critical() | `LOGGER_VAR_PATTERN\.critical\(` | mechanical |
| L8 expensive debug | `LOGGER_VAR_PATTERN\.debug\(.*(?:json\.dumps\|repr\|\.to_dict\|\.model_dump)` | heuristic |
| L9 warn-then-raise | multiline: log call within 3 lines before `raise` | heuristic |
| L10 credential in log | `LOGGER_VAR_PATTERN\.\w+\(.*(?:password\|secret\|token\|api_key\|credential\|bearer\|private_key)` (case-insensitive) | heuristic |
| L11 concatenation | `LOGGER_VAR_PATTERN\.\w+\(["'].*["']\s*\+` | mechanical |
| L12 %-style (non-stdlib only) | `LOGGER_VAR_PATTERN\.\w+\(["'].*%[sdfr]` — skip if `LOGGER_FRAMEWORK == stdlib` | heuristic |
| L18 exception() outside except | `LOGGER_VAR_PATTERN\.exception\(` | heuristic |
| L24 kwargs in application log calls | `LOGGER_VAR_PATTERN\.\w+\([^)]*,\s*\w+=` (non-stdlib only) — collect hits for Step 2.3 | heuristic |

**Framework-specific patterns (run only for the indicated framework):**

| Pattern | Grep | Framework | Type |
|---------|------|-----------|------|
| L13 extra reserved key | `extra=\{` — check keys against 22 reserved list | stdlib | heuristic |
| L14 arbitrary kwargs | `LOGGER_VAR_PATTERN\.\w+\([^)]*,\s*\w+=` | stdlib | heuristic |
| L15 event= kwarg | `LOGGER_VAR_PATTERN\.\w+\(.*event=` | structlog | mechanical |
| L16 dictConfig disable_existing | `dictConfig\(` | stdlib | heuristic |
| L17 basicConfig no-op | `basicConfig\(` (collect all across codebase) | stdlib | heuristic |
| L19 bind() discarded | `^\s+\w+\.bind\(` as bare statement | structlog/loguru | heuristic |
| L20 propagate=False | `propagate\s*=\s*False` | stdlib | heuristic |
| L21 logger.remove() | `logger\.remove\(\s*\)` (no args) | loguru | mechanical |
| L22 loguru kwargs as format | `LOGGER_VAR_PATTERN\.\w+\("[^"]*"[^)]*,\s*\w+=` | loguru | heuristic |
| L23 warn() deprecated | `\.warn\(` | stdlib | mechanical |

For **heuristic patterns**: collect `file:line` hits for Step 2.3 context reading.

For **mechanical patterns** (L1, L2, L5, L7, L11, L15, L21, L23): record directly as findings.

#### Step 2.3 — Context Reading and Classification

For each heuristic hit, read 10–15 lines of surrounding context.

Classify each hit:
- **Severity:** apply the severity from the pattern catalogue, adjusted by context
- **Legitimacy:** genuine finding or acceptable pattern (see §Legitimacy in `logging-patterns.md`)

**Universal framework adjustments:**
- L3: if `LOGGER_FRAMEWORK == stdlib` → ACCEPTABLE. If structlog/loguru → MEDIUM. Fix direction: %-style message body, not "unwrap to kwargs".
- L12: if `LOGGER_FRAMEWORK == stdlib` → ACCEPTABLE. If the log call goes through `CANONICAL_FACTORY` (e.g. `get_logger()`) AND that factory has a %-style bridge (look for `_format_printf_args` or similar in the adapter) → ACCEPTABLE. If the log call uses a direct loguru import (`from loguru import logger` / `loguru.logger`) even when a bridge adapter exists → HIGH (the bridge is bypassed; positional args are silently dropped because loguru uses `str.format()` not `%`). If vanilla loguru (no bridge) → HIGH. If vanilla structlog → MEDIUM.
- L10: read context — `token_name=name` is acceptable; `token=value` is CRITICAL.
- L6: if loop is clearly bounded to ≤10 items → ACCEPTABLE.
- L18: read 5 lines before the `.exception()` call — if inside an `except` block → ACCEPTABLE.
- L24: skip `exc_info=True/False`. Skip files that define the logging factory/adapter (the file containing `get_logger` or the logger adapter class). Flag all other kwargs as MEDIUM.

**Framework-specific classification rules:**
- L13: Read the `extra={}` dict literal. Flag CRITICAL for each key that matches any of the 22 reserved LogRecord attributes. Keys not in the reserved list → ACCEPTABLE.
- L14: Read the log call kwargs. Flag CRITICAL for any kwarg not in `{exc_info, extra, stack_info, stacklevel}`. Do NOT flag structlog or loguru projects.
- L16: Check the config dict passed to `dictConfig()`. If `"disable_existing_loggers"` is absent or `True` → HIGH. If `False` → ACCEPTABLE.
- L17: Collect all `basicConfig()` calls across the codebase. If more than one → flag the second+ as HIGH. Single call in `if __name__ == "__main__":` → ACCEPTABLE.
- L19: Check if the `.bind()` call is a bare statement (result not assigned) → HIGH. If assigned (`x = logger.bind(...)`) → ACCEPTABLE.
- L20: Read 10 lines around `propagate = False`. Check if `addHandler()` is called on the same logger. If no handler → HIGH. If handler present → ACCEPTABLE.
- L22: Read the message string. If any kwarg name has no matching `{kwarg_name}` placeholder in the message → MEDIUM. If all kwargs have placeholders → ACCEPTABLE (used for formatting).

Apply `SEVERITY_FILTER`: drop findings below the threshold.

#### Step 2.4 — Build Remediation Plan

For each genuine finding, produce an entry:
- **Location:** `file:line`
- **Severity:** CRITICAL / HIGH / MEDIUM / LOW
- **Category:** L1–L23 code + name
- **Current code:** offending lines
- **Fix template:** reference to FT-L1 through FT-L23 from `logging-patterns.md`
- **Auto-fixable:** yes/no

Group into priority tiers:
- **P0** — Fix Immediately: CRITICAL — L10 (credential leak), L13 (reserved key crash), L14 (stdlib kwargs crash)
- **P1** — Fix This Sprint: HIGH — L1, L2, L4, L15, L16, L17, L19, L20, L21
- **P2** — Fix When Touching File: MEDIUM/LOW — L3, L5, L6, L7, L8, L9, L11, L12, L18, L22, L23, L24

Also compute linting recommendations: read `pyproject.toml` for existing ruff or flake8 rule
selections. List any missing rules from §Linting Rules in `logging-patterns.md`:
- G001, G003, G004, T201, LOG009 — recommend if not present
- G002 — do NOT recommend (%-style is the preferred formatting; G002 would produce false positives)
- LOG015 — recommend if stdlib and ruff version supports it

Include the minimal `pyproject.toml` diff and suggested `lint-logging` Makefile target in the plan.

#### Step 2.5 — Enter Plan Mode

Call `EnterPlanMode`.

Present the Tune Report as the plan:

```markdown
# Tune Report — <TARGET_PATH>

## Project Logging Profile
- Framework: <LOGGER_FRAMEWORK>
- Canonical factory: <CANONICAL_FACTORY>
- Supports kwargs: <yes/no>
- Logger variable names found: logger, log, ...

## Executive Summary
- Files scanned: N
- Total hits: N (N genuine findings, N acceptable patterns)
- By severity: CRITICAL: N | HIGH: N | MEDIUM: N | LOW: N
- Auto-fixable: N | Needs manual review: N

## Logger Consistency
- Files using canonical factory: N
- Files using non-canonical factory: N (list)

## Findings
| # | File | Line | Severity | Pattern | Auto-fixable | Notes |
|---|------|------|----------|---------|--------------|-------|

## Remediation Plan

### P0 — Fix Immediately (CRITICAL)
[L10, L13, L14 findings with exact lines and fix template]

### P1 — Fix This Sprint (HIGH)
[L1, L2, L4, L15, L16, L17, L19, L20, L21 findings]

### P2 — Fix When Touching File (MEDIUM/LOW)
[L3, L5, L6, L7, L8, L9, L11, L12, L18, L22, L23 findings]

## Linting Recommendations
[pyproject.toml diff]
[Makefile target suggestion]

## Acceptable Patterns (no action required)
| File | Line | Pattern | Justification |
|------|------|---------|---------------|
```

Call `ExitPlanMode` to request user approval. Wait for approval before proceeding to Stage 2.

---

### Stage 2 — Execution (after plan approval)

Apply the remediation plan that was approved. Only fix what was listed in the approved plan.

#### Auto-fixable patterns — apply directly:
- **FT-L1:** f-string → rewrite as %-style message body (embed context in message, not kwargs)
- **FT-L2:** Import swap to canonical factory
- **FT-L4:** Add `exc_info=True` to `logger.warning(` / `logger.error(` in except blocks
- **FT-L5:** Simple `print("...")` → `logger.info("...")` using %-style for any embedded values
- **FT-L7:** `.critical(` → `.error(`
- **FT-L11:** String concatenation → %-style message body
- **FT-L14:** Wrap stdlib log call kwargs in `extra={}` (stdlib only)
- **FT-L16:** Add `"disable_existing_loggers": False` to dictConfig call
- **FT-L19:** Capture `bind()` return value — `logger.bind(...)` → `logger = logger.bind(...)`
- **FT-L22:** loguru kwargs → %-style message body
- **FT-L23:** `.warn(` → `.warning(`
- **FT-L24:** Application kwargs → %-style message body (keep `exc_info=True`)

**Do not auto-fix L10 (credential leak)** — always flag for human review only.

#### Manual review required — leave TODO comments:
- FT-L3, FT-L6, FT-L8, FT-L9, FT-L10, FT-L12, FT-L13, FT-L15, FT-L17, FT-L18, FT-L20, FT-L21

TODO comment format:
```python
# TODO(signal-over-noise): [L6] INFO inside loop — consider DEBUG + summary. See references/logging-patterns.md#L6
```

#### Apply linting config changes (if approved in the plan):
Update `pyproject.toml` with the recommended ruff rules.

#### After applying fixes:

Run pre-commit on every file that was changed:
```bash
uv run pre-commit run --files <file1> <file2> ...
```

If pre-commit is not available, skip this step (do not fail).

Leave all changes **uncommitted** — the user reviews before committing.

#### Finish

Report a brief summary: N files changed, N auto-fixes applied, N TODO comments added.

---

## Key Design Notes

1. **Plan mode is the fix gate.** The user approves the remediation plan before any files are
   touched. The plan shows exact file:line locations, current code, and proposed fixes.

2. **Two invocations, not one.** Surface and Tune are separate `--mode` values. This keeps each
   plan focused and reviewable. Run Surface first; its execution recommends Tune as the next step.

3. **Framework-agnostic:** Tune's Step 2.1 discovers the logging framework and canonical factory
   from the codebase itself. Nothing is hardcoded.

4. **kwargs are a universal anti-pattern in application code:** Always embed context in the
   message body via %-style. Framework context (Temporal workflow_id, run_id, activity_type,
   task_queue, attempt, etc.) is auto-injected by the logging adapter — never pass it manually.
   All other application kwargs land in an unindexed JSON blob in the observability platform,
   making them invisible to log stream scanning. The only accepted kwarg is `exc_info=True`.
   L24 detects kwargs in application log calls (non-stdlib, outside the adapter file itself).

   **L3 and L12 are still framework-dependent for classification:** `extra={}` is correct in
   stdlib and wrong in structlog/loguru (use %-style body instead). %-style is the universal
   preferred format; in vanilla loguru (without a custom adapter bridge) %-style args are
   silently dropped — flag as HIGH. In stdlib and projects with a custom %-bridge adapter,
   %-style is already correct.

5. **L13 and L14 are crash-level stdlib patterns:** `extra={}` key collisions with reserved
   LogRecord attributes raise `KeyError` that propagates to the caller. Treat as P0.

6. **L10 is security-critical:** Never auto-fix credential patterns. Always flag for human review.

7. **Phase ordering matters:** Run Surface before Tune. A silent-swallow in an except block (P1/P2)
   overlaps with a missing-exc_info finding (L4). Surface fixes the structural problem; Tune then
   verifies the logging quality of the fix. Avoid double-reporting the same line.

8. **Typed-error prescription is mandatory.** Every fix that re-raises must name an `AppError`
   leaf from `application_sdk.errors`. The skill never recommends raising a bare builtin or a
   legacy `AtlanError`. The leaf catalogue (§2), litmus tests (§3), SDK-context cookbook (§4),
   and exhaustive legacy-constant migration table (§5) all live in
   `references/typed-error-prescription.md`. P12 (untyped builtin raises) and P13 (legacy
   `AtlanError` raises) fold the BLDX-1261 audit into surface mode — every finding gets a
   `Prescribed:` entry naming the target leaf and suggested code string.
