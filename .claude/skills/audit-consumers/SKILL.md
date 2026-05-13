---
name: audit-consumers
description: >
  Audit every github.com/atlanhq/ consumer of application_sdk against a user-supplied check
  specification. Discovers consumer repos via `gh search code`, applies one or more named
  checks per repo (grep patterns anchored to SDK symbol usage), and produces a markdown
  report — including a coverage table listing every analyzed repo with its outcome
  (no-usage / no-findings / has-findings). Use when proposing an SDK breaking change,
  removing a deprecated symbol, or proactively measuring the blast radius of a refactor
  before merging.
argument-hint: "[--spec <path>] [--report <path>] [--sdk-major <N>]"
mandatory_triggers:
  - "/audit-consumers"
  - "audit application_sdk consumers"
  - "audit SDK consumers"
  - "check consumers of application_sdk"
optional_triggers:
  - "blast radius of this SDK change"
  - "who uses this SDK symbol"
  - "which apps would break if"
owner: connector-platform-team
last_updated: "2026-05-13"
staleness_days: 90
inputs:
  - spec: "path to a check-spec markdown file, OR inline natural-language description from the invoker"
  - report: "output path for the report markdown (default: <repo-root>/<slug>-consumer-audit.md)"
outputs:
  - markdown report at the specified path
  - inline executive summary (top findings) in chat
gates:
  - "Never modify consumer repos — read-only clone/API access only."
  - "Coverage table must list every candidate repo from Phase A, even when no findings."
  - "The only file written is the report — no edits to application_sdk source."
---

# Skill: `/audit-consumers`

Discovers every `github.com/atlanhq/` consumer of `application_sdk`, applies a user-defined
check specification across each consumer, and produces a markdown report that accounts for
every candidate repo — including those with no relevant usage — so the audit is demonstrably
holistic.

**Typical use cases:**
- "Which apps still import deprecated symbol `X` that we are removing in v4.0?"
- "Who catches exceptions from `BaseSQLClient.load()` narrowly, by class or message string?"
- "Which consumers pin the SDK to a version before feature `Y` shipped?"
- "What is the blast radius if we change the signature of `ObjectStore.upload_file()`?"

---

## Invocation

```
/audit-consumers                          # prompts for a spec inline
/audit-consumers --spec ./my-spec.md     # reads spec from file
/audit-consumers --report ./out.md       # custom report path
/audit-consumers --sdk-major 3           # explicitly set the target major version
```

Argument parsing (left-to-right, first match wins):
- `--spec <path>` — read check specification from this file instead of prompting
- `--report <path>` — write the markdown report here; default: `<repo-root>/<slug>-consumer-audit.md`
  where `<slug>` is the spec title lowercased, spaces replaced with hyphens
- `--sdk-major <N>` — integer target major version to audit against; repos pinned to other
  majors are recorded as `skipped-major-mismatch` without file analysis (see Phase 0b)

---

## Phase 0 — Capture the check specification

**If `--spec <path>` was provided:** read the file. Validate that it contains at least one
anchor pattern and at least one named check. If malformed, report the exact problem and stop.

**Otherwise:** ask the user to describe what they want to check. Convert the description into a
structured spec by echoing it back in the format below for confirmation.

> **Gate:** confirm the spec with the user before running any GitHub queries. This is the
> **only** interactive step — once the spec is locked, Phases A–C run to completion without
> further prompts.

### Check-spec format

The spec is a markdown document with this structure. Store it in chat if entered inline; save
to the path given by `--spec` if a file was provided.

```markdown
# Check spec — <Short title, used as the slug for the report filename>

## Context
<1–3 sentences: why this audit exists, what SDK change or question prompted it.>

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

## Phase 0b — Determine target major version

This phase runs once, before Phase A. Its output is a single integer `target_major` that
gates the per-repo analysis in Phase B.

### Resolution order (first hit wins)

1. **CLI flag:** if `--sdk-major <N>` was passed, use that integer directly.
2. **Local SDK tree:** read `pyproject.toml` in the current working directory (normally the
   `application-sdk` checkout) and extract `[project].version`. Take the first numeric
   component before the first `.`:
   ```python
   import re, tomllib
   with open("pyproject.toml", "rb") as f:
       version = tomllib.load(f)["project"]["version"]
   target_major = int(re.match(r"(\d+)", version).group(1))
   ```
3. **Fallback:** if neither source is available, prompt the user once for the target major
   during Phase 0 spec confirmation: "Which major version of the SDK are you auditing
   against? (e.g. 3)"

Surface the resolved value in chat: `Target major version: 3 (from pyproject.toml)`.

### Pin-parsing rules

When processing each repo in Phase B, parse the discovered SDK pin string into a set of
compatible major versions using the following rules:

| Pin string | Compatible majors |
|---|---|
| `3.8.0`, `==3.0.0`, `3.0.0a1`   | `{3}` |
| `2.7.4`, `==2.5.0`              | `{2}` |
| `>=3.0.0,<4.0.0`, `>=3.5.0,<4` | `{3}` |
| `>=3.6.0,<5`                    | `{3, 4}` |
| `>=2.8.0` (no upper bound)      | `{2, 3, 4, …}` — treat as current major and later; always includes `target_major` |
| git tag `v3.7.0`                | `{3}` |
| git branch `release/v2`         | `{2}` |
| git branch `main`               | `{target_major}` — tip-of-default-branch tracks current major |
| path editable `../application-sdk` | `{target_major}` — local dev, assume current major |
| missing / unparseable           | unknown — fall through to analysis; status becomes `analyzed-pin-unknown` |

Implementation pseudocode (inline, not stored as a separate file):

```python
import re

def compatible_majors(pin: str, target: int):
    """Return set of compatible majors, or None if unparseable."""
    pin = pin.strip()
    # Editable / path install
    if pin.startswith(("../", "./", "/")):
        return {target}
    # Default-branch markers
    if pin in ("main", "master", "develop"):
        return {target}
    # Branch: release/vN
    m = re.match(r"release/v(\d+)", pin)
    if m:
        return {int(m.group(1))}
    # Git tag: vN.M.P
    m = re.match(r"v(\d+)\.", pin)
    if m:
        return {int(m.group(1))}
    # Commit hash — unknown which branch
    if pin.startswith("rev:"):
        return None
    # Extract operator and lower-bound major
    first = pin.split(",")[0].strip()
    op_m = re.match(r"^([=!<>~^]+)", first)
    operator = op_m.group(1) if op_m else ""
    first_clean = re.sub(r"^[=!<>~^]+", "", first).strip()
    m = re.match(r"(\d+)", first_clean)
    if not m:
        return None
    lower = int(m.group(1))
    # Exact pin: == means exactly this major
    if operator == "==":
        return {lower}
    # Upper bound: <N means up to (not including) major N
    # If upper bound has the same major as lower (e.g. >=3.6.1,<3.7.0),
    # it's a minor/patch constraint — still only major lower.
    ub = re.search(r",\s*<\s*(\d+)", pin)
    if ub:
        upper = int(ub.group(1))
        if upper > lower:
            return set(range(lower, upper))
        else:
            return {lower}   # same-major minor/patch constraint
    # No upper bound — includes lower through target and beyond
    return set(range(lower, target + 1))
```

If `compatible_majors()` returns `None`, proceed with analysis and record status
`analyzed-pin-unknown`. If it returns a set that does **not** contain `target_major`,
record status `skipped-major-mismatch` and stop processing that repo.

---

## Phase A — Discover candidate repos

Run four discovery signals in parallel, union the results:

```bash
gh search code "from application_sdk"   --owner atlanhq --limit 100 \
  --json repository --jq '.[].repository.nameWithOwner' | sort -u >  /tmp/audit-repos.txt

gh search code "import application_sdk" --owner atlanhq --limit 100 \
  --json repository --jq '.[].repository.nameWithOwner' | sort -u >> /tmp/audit-repos.txt

gh search code "application-sdk" --filename pyproject.toml --owner atlanhq --limit 100 \
  --json repository --jq '.[].repository.nameWithOwner'             >> /tmp/audit-repos.txt

gh search code "application-sdk" --filename requirements.txt --owner atlanhq --limit 100 \
  --json repository --jq '.[].repository.nameWithOwner'             >> /tmp/audit-repos.txt

sort -u /tmp/audit-repos.txt -o /tmp/audit-repos.txt
```

For each anchor pattern in the spec, also run:

```bash
gh search code "<anchor>" --owner atlanhq --limit 100 \
  --json repository --jq '.[].repository.nameWithOwner' >> /tmp/audit-repos.txt
sort -u /tmp/audit-repos.txt -o /tmp/audit-repos.txt
```

This catches repos whose code directly uses the anchored symbol even if their dependency
declarations are non-standard.

### Connector-map cross-check

Read the curated list of all connector/app repos:

```bash
gh api repos/atlanhq/integration-studio-aisdlc/contents/context/connector-map.md \
  --jq '.content' | base64 -d > /tmp/connector-map.md
```

Extract every `atlanhq/<repo-name>` that appears in that file and diff against
`/tmp/audit-repos.txt`. For each repo in the connector map that is **not** in the candidate
list, add it and log a warning:

```
⚠ Gap: atlanhq/<repo> appears in connector-map.md but was not found by gh search code.
  Added to candidate list. Cause: repo may not have searchable code indexed, or the SDK
  dependency uses a non-standard declaration format.
```

This guards against `gh search code` truncation, indexing lag, or repos whose only SDK
reference is in a non-Python file.

**Record every repo in the final candidate list verbatim.** The Phase C coverage table must
account for every entry, even ones that turn out to have no SDK usage at all.

---

## Phase B — Per-repo audit

For each candidate repo in `/tmp/audit-repos.txt`:

### Step 1 — Read SDK pin and apply major-version gate

```bash
gh api repos/atlanhq/<repo>/contents/pyproject.toml --jq '.content' | base64 -d \
  | grep -A2 'application.sdk\|application-sdk'
# fall back to requirements.txt if pyproject.toml is absent:
gh api repos/atlanhq/<repo>/contents/requirements.txt --jq '.content' | base64 -d \
  | grep 'application.sdk\|application-sdk'
```

Record the raw pin string for the coverage table. Then call `compatible_majors(pin,
target_major)` (see Phase 0b pseudocode):

- **Pin parses + target_major NOT in compatible set** → status `skipped-major-mismatch`.
  Record in coverage table with the pin string and compatible majors set. **Stop — do not
  run anchor check or fetch any files.**
- **Pin unparseable / missing** → proceed with analysis; final status will be
  `analyzed-pin-unknown`.
- **Pin parses + target_major in compatible set** → proceed to Step 2.

If the `gh api` call for both pin files fails → record status `analyzed-error` with the
error. Never silently drop.

### Step 2 — Anchor check (cheap, via GitHub API)

```bash
gh search code "<anchor>" --repo atlanhq/<repo> --json path,textMatches --limit 20
```

Run this for each anchor pattern in the spec. If all return empty → this repo has no
anchor-matched files. Record status `analyzed-no-usage`, add to coverage table, continue to
the next repo.

If `gh api` fails (private repo, rate limit, 404) → record status `analyzed-error` with the
error message. Never silently drop.

### Step 3 — Fetch anchored files

For each unique file path surfaced by the anchor search:

```bash
gh api repos/atlanhq/<repo>/contents/<path> --jq '.content' | base64 -d > /tmp/audit-file.py
```

**Clone fallback:** if more than 5 files need individual fetching, OR if the spec requires
cross-file analysis (e.g., subclass tracing), do a shallow clone instead:

```bash
git clone --depth 1 git@github.com:atlanhq/<repo>.git /tmp/sql-audit/<repo>
```

### Step 4 — Run checks

For each check in the spec, against each fetched file:

```bash
rg -nP '<pattern>' /tmp/audit-file.py
```

Record every hit as `(file_path, line_number, matched_line, check_id)`. For hits where
`require_anchor: false`, run the patterns against all Python files in the repo, not just
anchored ones (requires a clone or a separate `gh search code` call per pattern).

### Step 5 — Classify status

| Status | Condition |
|---|---|
| `analyzed-no-usage` | major-match; no anchor match anywhere in the repo |
| `analyzed-no-findings` | major-match; anchor present, zero check hits |
| `analyzed-has-findings` | major-match; at least one check hit |
| `skipped-major-mismatch` | pin parsed; compatible majors do not include `target_major` |
| `analyzed-pin-unknown` | pin missing or unparseable; analyzed conservatively |
| `analyzed-error` | GitHub API call failed (log the error) |

---

## Phase C — Synthesise and write the report

Write the report to the path specified by `--report` (or the default slug-based path).

### Required report sections

```markdown
# <Spec title> — Consumer Audit

**Audit date:** <ISO date>
**Audited by:** automated gh search + per-file content inspection

---

## Executive summary

| Metric | Count |
|---|---|
| Candidate repos discovered | N |
| Skipped (major mismatch — not on v`<target_major>`) | N |
| Pin unknown (analyzed conservatively) | N |
| Repos with anchor-matched files | N |
| Repos with findings | N |
| <check_id> hits (<impact>) | N across M repos |
| … | … |

<2–3 sentence narrative: what matters, what's safe, what needs action. Include a note like:
"Skipped N repos pinned to other majors — those will be re-audited when they migrate to
v<target_major>.">

---

## Spec used

<Verbatim copy of the check spec from Phase 0, so the report is self-contained.>

---

## Coverage — every repo we looked at

Every candidate repo from Phase A is listed here regardless of outcome. Repos are sorted
alphabetically. `analyzed-error` rows include the error so follow-up is possible. The
"Compatible majors" column shows the parsed set from the pin string so the filter decision
is auditable; `?` means unparseable.

| Repo | SDK pin | Compatible majors | Status |
|---|---|---|---|
| atlanhq/<repo-a> | `>=3.0.0,<4.0.0` | {3} | analyzed-has-findings |
| atlanhq/<repo-b> | `2.7.4`          | {2} | skipped-major-mismatch |
| atlanhq/<repo-c> | (none)           | ?   | analyzed-pin-unknown |
| atlanhq/<repo-d> | `3.0.0a1`        | {3} | analyzed-no-usage |
| atlanhq/<repo-e> | —                | —   | analyzed-error: 403 from gh api |

---

## Per-repo findings (only repos with hits)

### atlanhq/<repo-name>

- **SDK pin:** `<version>`
- **Anchor files:**
  - `<path/to/file.py>` (line N: `<matched line>`)

#### <check_id> — <check name> (impact: <impact>)

- `<file>:<line>` — `<matched line>`
  - Recommendation: <from spec>

---

## Repos added from connector-map cross-check

<List any repos that were added from the connector-map but were absent from gh search results,
with the gap warning. Omit this section if there were no gaps.>
```

### Inline summary in chat

After writing the report, print to chat:

1. The executive summary table.
2. The top 3–5 highest-impact hits (definite-break first, then silent-break, then review),
   with file:line and recommendation inline.
3. The report file path.

---

## Phase D — Optional verification (on request)

These steps are offered but NOT run automatically. Invoke them by asking:
`"verify the top findings"` or `"show me the GitHub links"`.

### D1 — Spot-check via GitHub URLs

For each top finding, construct the direct link:
```
https://github.com/atlanhq/<repo>/blob/<default-branch>/<path>#L<line>
```
Open in browser or present as a list. Lets the reviewer confirm the grep match is not in a
comment or string literal, and that the `try` body actually calls the SDK method.

### D2 — Behaviour simulation

For checks with `impact: silent-break` (e.g., message-string inspection), demonstrate that
the old match would fail against a newly typed error:

```python
from application_sdk.errors.<module> import <NewErrorType>
e = <NewErrorType>(...)
assert "<old_fragment>" not in str(e), f"Still matches: {str(e)!r}"
```

Only run when explicitly requested — not all specs involve exception message changes.

---

## Appendix — Check-spec examples

### Example A: deprecated symbol removal

```markdown
# Check spec — ObjectStore.upload (legacy sync method removed in v4.0)

## Context
application_sdk v4.0 removes `ObjectStore.upload()` (sync). All callers must migrate to
`ObjectStore.upload_file()` (async). Audit before the removal PR merges.

## Anchors
- pattern: `from application_sdk\.storage`
- pattern: `ObjectStore`

## Checks

### check: R1 — Direct call to .upload()
- impact: definite-break
- require_anchor: true
- patterns:
  - `\.upload\s*\(`
- recommendation: replace `.upload(key, path)` with `await .upload_file(key, path)`
```

### Example B: exception type change

```markdown
# Check spec — BaseSQLClient error-type changes (PR #1234)

## Context
PR #1234 replaces ValueError/ClientError raises in BaseSQLClient with typed AppError subclasses.
Consumers catching by old type will silently stop receiving the error.

## Anchors
- pattern: `BaseSQLClient`
- pattern: `AsyncBaseSQLClient`
- pattern: `from application_sdk\.clients\.sql`

## Checks

### check: R1 — Narrow catch of old exception types
- impact: definite-break
- require_anchor: true
- patterns:
  - `except\s+(ValueError|ClientError|CommonError)\b`
- recommendation: update to catch the new typed error (e.g. `AuthError`, `InternalError`) or `AppError`

### check: R2 — Message-string inspection
- impact: silent-break
- require_anchor: false
- patterns:
  - `"DB_CONFIG is not configured"`
  - `SQL_CLIENT_AUTH_ERROR`
  - `CREDENTIALS_PARSE_ERROR`
- recommendation: switch to isinstance check against the new typed error class

### check: R3 — Broad except Exception near SDK calls
- impact: review
- require_anchor: true
- patterns:
  - `except\s+(Exception|BaseException)\b`
- recommendation: review whether the handler re-raises as a legacy type or inspects the message
```
