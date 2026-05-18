# Check-spec format

The check specification is a self-contained markdown document authored by the SDK engineer
running the audit. Phase 0 validates it before any GitHub queries run.

---

## Core structure

```markdown
# Check spec — <Short title, used as the slug for the report filename>

## Context
<1–3 sentences: why this audit exists, what SDK change or question prompted it.>

## target_release
<Free-form string naming the SDK release this audit is for — version tag, PR number, or
 commit SHA. Examples: "v4.0.0", "application_sdk#1234", "pre-release". Required when
 --raise-prs mode is used; prompted by Phase 0 if not supplied via --sdk-release.>

## Anchors

Files in a consumer repo are "in scope" only if they match at least one anchor. Anchors are
typically SDK imports or the name of the class/symbol under review. Every check with
`require_anchor: true` is only run against files that match an anchor.

- pattern: `from application_sdk\.<module> import`   # regex, applied with rg -P
- pattern: `BaseSQLClient`

## Checks

Each check is a named risk class with one or more grep patterns.

### check: R1 — <Name>
- impact: definite-break | silent-break | review
- require_anchor: true | false
- patterns:
  - `<regex 1>`   # regex, applied with rg -nP against anchored files
  - `<regex 2>`
- recommendation: <one-line suggested fix printed next to each hit in the report>
- fix:                   # optional — see "The fix: block" below
  - pattern_index: 0
    replace_regex: `<replacement>`
  add_import: `from application_sdk.<module> import <Symbol>`

### check: R2 — <Name>
- impact: silent-break
- require_anchor: false
- patterns:
  - `"<literal string fragment>"`
- recommendation: …
```

**Impact levels:**
- `definite-break` — the pattern will cause an exception/error at runtime after the SDK change
- `silent-break` — the pattern silently produces wrong results (e.g., string match fails)
- `review` — broad catch or comment that warrants a human look but may not break

---

## The optional `fix:` block

When `--raise-prs` is active, each check may carry an optional `fix:` block that tells Phase E
exactly how to rewrite a matched line. When `fix:` is absent, Phase E falls back to an
LLM-driven rewrite using the prose `recommendation:` plus the captured hit context
(see `references/fix-generation.md`).

### `fix:` entry fields

```yaml
- fix:
  - pattern_index: 0        # 0-based index into the check's `patterns:` list
    replace: `<literal>`    # literal string substitution on the matched span (no regex)
    # OR (mutually exclusive):
    replace_regex: `<str>`  # passed as the repl arg to re.sub; \1, \2 backrefs are supported
  add_import: `from application_sdk.errors import AppError`   # optional, check-level
  # OR (for multiple imports):
  add_imports:
    - `from application_sdk.errors import AppError`
    - `from application_sdk.errors import InternalError`
```

**Rules:**
- `replace` and `replace_regex` are **mutually exclusive**. Specifying both on a single
  entry is a validation error. `replace` is a plain string substitution (no pattern
  interpretation); `replace_regex` is passed directly as the `repl` argument to `re.sub`
  and may contain `\1`, `\2` backrefs into the match groups from `patterns[pattern_index]`.
- `pattern_index` must be in range `[0, len(patterns)-1]`. Out-of-range is a validation error.
- Patterns without a matching `pattern_index` in `fix:` use the LLM fallback for their hits.
  A single check can be **mixed-mode** (deterministic for some patterns, LLM for others).
- `add_import` / `add_imports` are **check-level** (not per-pattern): if any pattern in the
  check produces at least one applied fix in a file, the import is added once at the top of
  that file. Imports are deduped against existing import strings.
- Each `add_import(s)` value must be a syntactically valid Python import statement
  (`re.match(r'^(import|from)\s', s)` must match). Invalid → validation error.

**Phase 0 validation** (only when `--raise-prs` is set):

1. Check each `fix:` entry has exactly one of `replace`, `replace_regex`, `replace_template`.
2. Check each `pattern_index` is in range.
3. Check `add_import(s)` values parse as import statements.

Validation failures are reported with the exact check ID and field. Stop before Phase A —
no point auditing if the fixes are malformed.

### Applying deterministic fixes (E4a)

For each `confirmed` hit whose check has a matching `fix:` entry:

1. Compile `patterns[pattern_index]` with `re.compile(pattern, re.MULTILINE)`.
2. Apply `compiled_pattern.sub(replacement, matched_line)` against the **single captured
   line at `line_number`** — never the whole file. This avoids collateral matches elsewhere in the
   file that happen to match the same pattern.
3. If substitution produces zero changes (pattern has drifted from the captured line), demote
   this hit to the LLM fallback path and log a warning:
   `[Phase E] fix block for <check_id> didn't match captured line <file>:<line>. Falling back to LLM rewrite.`

### When to omit `fix:`

Omit the `fix:` block when the required change is too contextual for a regex:
- The fix depends on type information (e.g., "catch `AppError` but only if the enclosing
  function is a Temporal activity").
- The fix requires coordinating changes across multiple lines (e.g., unwrapping a `.message`
  chain into `isinstance`).
- You'd rather rely on Claude's judgment from the prose `recommendation:`.

The presence of a `fix:` block is an author signal that the transformation is mechanical and
safe to apply deterministically. When in doubt, omit it — the LLM fallback produces a
diff the author can review before the PR is opened.

---

## Optional triage override block

A check MAY include a `triage:` block for project-specific false-positive signals, evaluated
before the default heuristics in Phase B Step 4b:

```markdown
### check: R1 — Narrow catch of old exception types
…
- triage:
    false_positive_if_try_body_contains:
      - `urllib\.parse`
      - `CredentialRef\.resolve`
    confirmed_if_try_body_contains:
      - `\.run_query\s*\(`
      - `\.load\s*\(`
```

Spec heuristics take precedence over the defaults; defaults only fire when the spec rules
are silent.
