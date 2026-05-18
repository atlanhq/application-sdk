# Phase E fix generation

This reference covers the two paths Phase E uses to generate code changes for each confirmed
hit: the **deterministic path** (E4a, when `fix:` is present in the spec) and the **LLM
fallback path** (E4b, when it is not, or when E4a fails to match).

See `references/spec-format.md` for the `fix:` block syntax.

---

## E4a — Deterministic fix application

**When it runs:** the hit's check has a `fix:` entry with a `pattern_index` matching the
pattern that produced the hit.

**Algorithm:**

1. Retrieve the `fix:` entry for `pattern_index`.
2. Determine the replacement mode from the entry:
   - `replace:` → literal string substitution on the matched span
   - `replace_regex:` → `re.sub(compiled_pattern, replacement, matched_line)`
   - `replace_template:` → `re.sub(compiled_pattern, replacement, matched_line)` where the
     replacement string may reference `\1`, `\2`, … captured groups
3. Compile `patterns[pattern_index]` with `re.compile(pattern, re.MULTILINE)`.
4. Apply the substitution against **only the single captured line** at `line_number` in the
   in-memory copy of the file. Never apply to the whole file in bulk — avoids collateral
   matches elsewhere that happen to share the same pattern.
5. If `re.sub` produces zero changes (pattern has drifted since the Phase B hit was
   captured), demote to E4b and log:
   ```
   [Phase E] atlanhq/<repo>: fix block for <check_id> didn't match captured line
     <file>:<line>. Falling back to LLM rewrite for this hit.
   ```
6. Write the modified line back into the in-memory file. Collect all modified lines before
   writing the file to disk (process hits in descending line order so byte offsets stay valid
   if multiple hits are in the same file).

**Import insertion** (per file, after all hits in the file are processed):

For each check that produced at least one successfully applied E4a fix in this file, and has
a non-empty `add_import` / `add_imports`, insert the import(s) once:

1. Find the last `import …` or `from … import …` line in the file.
2. Insert after it, preserving one blank line if one exists above the block.
3. Dedup: if the exact import string already appears in the file, skip.
4. If no existing imports are found (unusual), insert after the module docstring / shebang
   (first non-blank, non-`#!`, non-docstring line).

---

## E4b — LLM fallback path

**When it runs:**
- The hit's check has no matching `fix:` entry, OR
- E4a was attempted and produced zero changes for this hit.

**One prompt per hit.** Never one prompt per file — keeping the scope tight makes diffs
reviewable and limits blast radius. Diffs that touch more than the hit's context window are
rejected (see below).

### Prompt template

```
You are patching a single Python file in a consumer of `application_sdk` to adapt it to an
upcoming SDK change. Apply the **minimum change** needed. Do not refactor unrelated code.

## SDK change context (from spec)
<spec.Context verbatim>

## Target release
<spec.target_release>

## Risk check that fired
- Check ID: <check_id>
- Impact: <impact>
- Recommendation: <recommendation verbatim>

## File: <repo-relative path>
## Hit line: <line_number>
## Matched line (verbatim):
    <matched_line>

## Context (3 lines before, matched line marked with >, 3 lines after — with line numbers):
<line_number-3>: <context_before[0]>
<line_number-2>: <context_before[1]>
<line_number-1>: <context_before[2]>
> <line_number>: <matched_line>
<line_number+1>: <context_after[0]>
<line_number+2>: <context_after[1]>
<line_number+3>: <context_after[2]>

## Enclosing symbol: <enclosing_symbol, or "module-level">

## Try-body excerpt (for except patterns only; empty otherwise):
<try_body_excerpt>

## Constraints — you MUST follow these exactly
1. Output ONLY a unified diff (`diff -u` format) against this exact file. No prose, no
   markdown fences, no explanation.
2. Modify ONLY the lines shown in the context window above, or add an import at the top of
   the file (after the last existing import line).
3. Do not rename variables, refactor unrelated logic, or change whitespace outside the
   modified lines.
4. Preserve indentation exactly — including tabs vs. spaces.
5. If the fix adds a new import, include it as a hunk in the same diff.
6. If you cannot produce a safe, minimal fix (e.g., the change is too ambiguous without
   broader context), output the literal string `ABSTAIN` and nothing else. Do not explain why.
```

### Response contract

The response is **either**:
- A valid unified diff in `diff -u` format (starting with `--- a/<path>` / `+++ b/<path>`), OR
- The literal string `ABSTAIN` (nothing else — no prose, no explanation).

Anything that does not match one of these two forms is treated as `ABSTAIN` with a warning
logged.

### Diff rejection rules

After receiving the LLM response, apply these checks **before** staging the patch. All checks
run against the raw diff string before `git apply` is attempted.

| Rule | Check | On failure |
|---|---|---|
| **Parseable** | Response starts with `---` and `+++` header lines | Treat as ABSTAIN |
| **In-scope files only** | All `+++ b/<path>` lines in the diff name `<repo-relative path>` only — no other files | Reject + unresolved TODO |
| **`git apply --check` passes** | `echo "<diff>" \| git apply --check -` exits 0 | Reject + unresolved TODO |
| **Scope creep** | Total lines added + removed (count `+` and `-` prefixed lines, not `@@` or `---`/`+++`) ≤ 8 | Reject + unresolved TODO (threshold tuneable) |
| **No mass deletion** | Lines removed ≤ 2 × (context window size) = 2 × 6 = 12 | Reject + unresolved TODO |

When a diff is rejected, the hit is added to the repo's **unresolved-hits list** with the
rejection reason. These surface as checked-off TODOs in the PR body. Do **not** retry the
LLM — a single attempt per hit, then human handoff. Loops compound hallucinations.

When the response is `ABSTAIN`, add the hit to the unresolved list with reason `LLM abstained`.

### Applying a valid diff

After all rejection checks pass:

```bash
echo "<diff>" | git apply --index -
```

`--index` stages the change immediately. After all hits in the repo are processed, run
`git diff --staged --stat` to confirm the change shape is coherent before showing the E6
diff preview to the user.
