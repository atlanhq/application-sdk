---
name: forward-port-to-v3
description: Analyze a GitHub release changelog URL and port any un-ported changes to the refactor-v3 branch of atlanhq/application-sdk
argument-hint: "<GitHub compare URL, e.g. https://github.com/atlanhq/application-sdk/compare/v2.8.0...v2.8.1>"
---

# /forward-port-to-v3

Analyze a GitHub release and forward-port any un-ported changes into the `refactor-v3` branch
of `atlanhq/application-sdk`. Presents a plan for user approval, then implements after confirmation.

**Must be run from the `application-sdk` repo root on the `refactor-v3` branch (or a branch
cut from it).**

## Usage

```
/forward-port-to-v3 https://github.com/atlanhq/application-sdk/compare/v2.8.0...v2.8.1
```

---

## Stage 1 — Analysis

### Step 1.1 — Parse the URL

Parse `$ARGUMENTS` to extract:
- `REPO` = `<owner>/<repo>` (e.g. `atlanhq/application-sdk`)
- `BASE` = the older tag/ref (e.g. `v2.8.0`)
- `HEAD` = the newer tag/ref (e.g. `v2.8.1`)

URL format: `https://github.com/<owner>/<repo>/compare/<base>...<head>`

If `$ARGUMENTS` is empty or malformed, stop and ask the user for the compare URL.

### Step 1.2 — Verify branch

Confirm the current branch is `refactor-v3` or a branch cut from it:

```bash
git branch --show-current
git log --oneline -5
```

If the working branch is unrelated (e.g. `main`), warn the user and ask whether to continue.

### Step 1.3 — Fetch the diff

Run both calls **in parallel**:

```bash
gh api repos/$REPO/compare/$BASE...$HEAD \
  --jq '.commits[] | {sha: .sha[0:8], message: .commit.message}'
```

```bash
gh api repos/$REPO/compare/$BASE...$HEAD \
  --jq '.files[] | {filename: .filename, status: .status, additions: .additions, deletions: .deletions, patch: .patch}'
```

### Step 1.4 — Categorize each changed file

Assign every file to exactly one category:

| Category | Description | Files |
|----------|-------------|-------|
| **A — Application code** | Must be ported or deliberately skipped | `application_sdk/`, `tests/`, `CLAUDE.md`, `docs/` (excluding version files) |
| **B — CI / tooling** | Check if v3 already has the equivalent | `.github/`, `.claude/commands/`, `.claude/skills/`, `Dockerfile*`, `helm/` |
| **C — Release artifacts** | Never port | `CHANGELOG.md`, `application_sdk/version.py`, version bumps in `pyproject.toml`/`uv.lock` |

### Step 1.5 — Check each A and B file against v3

For each A and B file, determine its port status. Run checks in parallel where possible.

**Check 1 — Git log match** (look for the PR number or a distinctive phrase from the commit message):
```bash
git log --oneline | grep -i "<pr-number-or-keyword>"
```

**Check 2 — File existence in v3**:
```bash
git ls-files <filename>
```

**Check 3 — Content check** (read the file and compare against the patch to determine if the
change is already applied). If the file does not exist in v3, note that it may need to be created
or that v3 restructured the path.

For files where you are unsure, read the surrounding context in the v3 source to understand
whether v3 already handles the concern in a different way.

Assign each file one of these statuses:

| Status | Meaning |
|--------|---------|
| `already-ported` | Equivalent change is in v3 (cite the commit) |
| `needs-porting` | Change not yet in v3; action required |
| `not-applicable` | v3 handles this differently or the concern is moot in v3 (explain why) |
| `release-artifact` | Version bumps, changelogs — never port |

For each `needs-porting` file, determine the porting action:

| Action | When to use |
|--------|-------------|
| `apply patch directly` | File exists in v3 largely unchanged; patch applies cleanly |
| `adapt and apply` | File exists but v3 has diverged; equivalent logic must be applied by hand |
| `create file` | File does not exist in v3 yet and should be created |

---

## Stage 1 — Plan Presentation

### Step 1.6 — Enter Plan Mode

Call `EnterPlanMode`.

Present the port plan in this format:

```markdown
# Forward-Port Plan: $BASE → $HEAD → refactor-v3

**Source:** `$BASE` → `$HEAD` (`$REPO`)
**Target:** `refactor-v3`

---

## Already Ported

| File | Commit | Notes |
|------|--------|-------|
| `application_sdk/foo.py` | `abc1234` | Ported as part of fix: suppress log pollution (#1149) |

## Needs Porting

| File | Action | Risk | Notes |
|------|--------|------|-------|
| `.github/workflows/publish-app.yaml` | `apply patch directly` | low | New workflow file; no v3 equivalent yet |
| `application_sdk/bar.py` | `adapt and apply` | medium | v3 uses different log adapter — update accordingly |

## Skipped

| File | Category | Reason |
|------|----------|--------|
| `CHANGELOG.md` | release artifact | v3 manages its own changelog |
| `application_sdk/version.py` | release artifact | v3 manages its own version |

---

## Summary

- Already ported: N files
- Needs porting: N files
- Skipped: N files
```

Call `ExitPlanMode` to request user approval. Wait for the user to respond before proceeding.

**If the user requests adjustments**, update the plan and call `EnterPlanMode` / `ExitPlanMode`
again until the user approves.

**If the user says no or cancels**, stop and explain what was not done.

---

## Stage 2 — Implementation (only after plan approval)

Work through the "Needs Porting" list **one file at a time** in the order presented.

### For `apply patch directly`

1. Read the current file content.
2. Apply the changes from the patch using Edit.
3. Run pre-commit on the file:
   ```bash
   uv run pre-commit run --files <file>
   ```
4. Fix any pre-commit issues before moving to the next file.

### For `adapt and apply`

1. Read the current v3 file carefully.
2. Read `docs/agents/coding-standards.md` if the file involves logging, error handling, or
   any SDK abstractions.
3. Apply the equivalent logic, adapting to v3 conventions:
   - Use `%-style` log messages — no f-strings in log calls, no kwargs except `exc_info=True`
   - Use `get_logger(__name__)`, not loguru directly
   - Use async-only Atlan clients — no sync `AtlanClient` or `run_in_thread` wrappers
   - Use `pyatlan_v9` import paths, not `pyatlan`
   - Default to `WARNING` for caught exceptions; `DEBUG` only for known-benign probes
4. Run pre-commit:
   ```bash
   uv run pre-commit run --files <file>
   ```

### For `create file`

1. Port the file, adapting any v2-specific patterns to v3 conventions (see rules above).
2. Run pre-commit:
   ```bash
   uv run pre-commit run --files <file>
   ```

### After all files are ported

Run the full pre-commit suite:
```bash
uv run pre-commit run --all-files
```

If any application code was changed, run unit tests:
```bash
uv run pytest tests/unit/ -x -q
```

Fix any failures before finishing.

### Final summary

Print a short summary of what was done:

```
## Forward-Port Complete: $BASE → $HEAD

### Changes applied
- `<file>`: <one-line description of what was ported>
- ...

### Pre-commit: passed
### Tests: N passed (or: skipped — no application code changed)

Changes are unstaged. Review with `git diff`, then use `/commit` to commit and push.
```

**Do NOT commit** — leave staging and committing to the user via `/commit`.

---

## Constraints

- **Never port version bumps.** `version.py`, version fields in `pyproject.toml`, `uv.lock`
  diffs, and `CHANGELOG.md` are always skipped — v3 manages its own versioning.
- **Always adapt, never copy blindly.** v2 code patterns (sync clients, loguru imports,
  f-string log calls, `pyatlan` imports) must be converted to v3 conventions.
- **Respect v3 design decisions.** If v3 replaced a v2 module entirely (e.g. a new abstraction
  covers the same concern), skip the port and explain why in the plan.
- **Pre-commit is mandatory.** Never move to the next file without passing pre-commit on the
  current one. CI will fail if pre-commit is not clean.
- **Plan mode gates all changes.** No files are touched until the user approves the plan.
