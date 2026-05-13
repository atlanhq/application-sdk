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
| git branch `main`               | `{target_major}` — tip of main tracks current major |
| git branch (any other name, incl. `master`, `develop`) | unknown — skip analysis; see `[tool.uv.sources]` dict rules below |
| path editable `../application-sdk` | `{target_major}` — local dev, assume current major |
| missing / unparseable (string pin) | unknown — fall through to analysis; status becomes `analyzed-pin-unknown` |

When the pin comes from a `[tool.uv.sources]` TOML dict (see Phase B Step 1), apply these
additional rules (evaluated before the string-form rules above):

| `[tool.uv.sources]` entry | Compatible majors | Coverage display string |
|---|---|---|
| `{git, tag = "vN.M.P"}`                                       | `{N}`      | `tag vN.M.P` |
| `{git, branch = "release/vN"}`                                | `{N}`      | `branch release/vN` |
| `{git, branch = "main"}`                                      | `{target}` | `branch main` |
| `{git, branch = <any other>}` (incl. `master`, `develop`, feature branches) | `None` | `branch <name>` |
| `{git, rev = "<hash>"}`                                       | `None`     | `rev <hash[:8]>` |
| `{path = "...", editable = true}` or `{path = "..."}`         | `{target}` | `editable <path>` |
| anything else                                                  | `None`     | the raw repr |

When `compatible_majors()` returns `None` for a dict-form pin, record status
`skipped-pin-source-unknown` and **stop** — do not analyze the repo. This is distinct from
string-form `None` (which falls through to `analyzed-pin-unknown` with conservative analysis)
because a rev hash or non-standard branch makes it impossible to know which SDK code the
consumer is actually running against.

Implementation pseudocode (inline, not stored as a separate file):

```python
import re

def compatible_majors(pin, target: int):
    """Return (set_of_compatible_majors_or_None, display_str).

    `pin` is one of:
      • str  — a PEP 440 specifier, branch name, git tag string, or path string
      • dict — a parsed [tool.uv.sources] entry, e.g.
               {"git": "...", "branch": "release/v2"}
               {"git": "...", "rev":    "91e9c4a9..."}
               {"git": "...", "tag":    "v3.7.0"}
               {"path": "../application-sdk", "editable": True}

    Returns:
      (set_or_None, display_str)
      • set  — compatible major versions
      • None — major undetermined (behaviour depends on pin type: str → analyze
                conservatively; dict → skip; see Phase B Step 1 gate table)
    """
    # --- Dict form: [tool.uv.sources] entry ---
    if isinstance(pin, dict):
        tag    = pin.get("tag")
        branch = pin.get("branch")
        rev    = pin.get("rev")
        path   = pin.get("path")

        if tag:
            m = re.match(r"v(\d+)\.", str(tag))
            if m:
                n = int(m.group(1))
                return {n}, f"tag {tag}"
            return None, f"tag {tag}"

        if branch:
            if branch == "main":
                return {target}, f"branch {branch}"
            m = re.match(r"release/v(\d+)", branch)
            if m:
                n = int(m.group(1))
                return {n}, f"branch {branch}"
            # Any other branch (master, develop, feature/…) — unknown major
            return None, f"branch {branch}"

        if rev:
            short = str(rev)[:8]
            return None, f"rev {short}"

        if path:
            return {target}, f"editable {path}"

        return None, repr(pin)

    # --- String form: [project].dependencies / requirements.txt ---
    pin = str(pin).strip()
    display = pin

    if pin.startswith(("../", "./", "/")):
        return {target}, display
    if pin == "main":
        return {target}, display
    # release/vN branch
    m = re.match(r"release/v(\d+)", pin)
    if m:
        return {int(m.group(1))}, display
    # Git tag: vN.M.P
    m = re.match(r"v(\d+)\.", pin)
    if m:
        return {int(m.group(1))}, display
    # Commit hash marker — unknown major
    if pin.startswith("rev:"):
        return None, display
    # PEP 440 specifier
    first = pin.split(",")[0].strip()
    op_m = re.match(r"^([=!<>~^]+)", first)
    operator = op_m.group(1) if op_m else ""
    first_clean = re.sub(r"^[=!<>~^]+", "", first).strip()
    m = re.match(r"(\d+)", first_clean)
    if not m:
        return None, display
    lower = int(m.group(1))
    if operator == "==":
        return {lower}, display
    ub = re.search(r",\s*<\s*(\d+)", pin)
    if ub:
        upper = int(ub.group(1))
        if upper > lower:
            return set(range(lower, upper)), display
        else:
            return {lower}, display   # same-major minor/patch constraint
    return set(range(lower, target + 1)), display
```

Gate decisions after calling `compatible_majors(pin, target_major)`:

| Result | Pin source | Action |
|---|---|---|
| Set contains `target_major` | either | Proceed to Step 2 |
| Set is non-empty, excludes `target_major` | either | Status `skipped-major-mismatch` — stop |
| `None` | dict (`[tool.uv.sources]` rev or unknown branch) | Status `skipped-pin-source-unknown` — stop |
| `None` | str (`[project].dependencies` / `requirements.txt`) | Status `analyzed-pin-unknown` — proceed conservatively |
| API call failed | — | Status `analyzed-error` — stop |

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

Fetch `pyproject.toml` first; fall back to `requirements.txt` if absent. Parse in two
stages using the Python helper that is already running in this phase:

```python
import base64, re, subprocess

# --- Fetch pyproject.toml ---
raw = subprocess.run(
    ["gh", "api", f"repos/atlanhq/{repo}/contents/pyproject.toml",
     "--jq", ".content"],
    capture_output=True, text=True,
)
text = base64.b64decode(raw.stdout.strip()).decode() if raw.returncode == 0 else ""

# --- Stage 1: [tool.uv.sources] (authoritative when present) ---
uv_source = None
src_block = re.search(
    r"\[tool\.uv\.sources\](.+?)(?:\n\[|\Z)", text, flags=re.DOTALL,
)
if src_block:
    for line in src_block.group(1).splitlines():
        line = line.split("#", 1)[0].strip()   # drop comments; skip blanks
        if not line:
            continue
        m = re.match(
            r'(?:atlan[-_]application[-_]sdk|application[-_]sdk)\s*=\s*\{(.+)\}',
            line, flags=re.IGNORECASE,
        )
        if m:
            uv_source = {}
            for kv in re.finditer(
                r'(\w+)\s*=\s*(?:"([^"]*)"|(true|false))', m.group(1)
            ):
                uv_source[kv.group(1)] = (
                    kv.group(2) if kv.group(2) is not None
                    else (kv.group(3) == "true")
                )
            break   # first uncommented match wins; ignore commented alternatives

# --- Stage 2: [project].dependencies or requirements.txt (fallback) ---
project_pin = None
if uv_source is None and text:
    # look for the SDK dep inside the [project] or [project.dependencies] block
    m = re.search(
        r'["\']atlan[-_]application[-_]sdk[^"\']*["\']', text, re.IGNORECASE
    )
    if m:
        # extract the version specifier portion, e.g. ">=3.0.0,<4.0.0"
        ver = re.search(r'((?:[><=!~^,\s\d\.\*]+)+)', m.group().split("[")[0].split('"')[-1])
        project_pin = ver.group(1).strip() if ver else None

if uv_source is None and project_pin is None and not text:
    # Try requirements.txt
    req = subprocess.run(
        ["gh", "api", f"repos/atlanhq/{repo}/contents/requirements.txt",
         "--jq", ".content"],
        capture_output=True, text=True,
    )
    if req.returncode == 0:
        req_text = base64.b64decode(req.stdout.strip()).decode()
        m = re.search(r'atlan[-_]application[-_]sdk([^\n]*)', req_text, re.IGNORECASE)
        project_pin = m.group(1).strip() if m else None

pin = uv_source if uv_source is not None else project_pin
majors, pin_display = compatible_majors(pin, target_major)
```

**Key rules for the two-stage parse:**
- `[tool.uv.sources]` overrides `[project].dependencies` — when an entry for the SDK is
  found in the sources table, use it exclusively. The deps entry typically carries no
  version specifier when a source override is present.
- Match the SDK package under either `atlan-application-sdk` or `application-sdk`
  (case-insensitive, hyphen or underscore).
- Commented-out lines (common for keeping an editable-path fallback around) are stripped
  before matching — only the active, uncommented source is used.
- `pin_display` (from `compatible_majors`) is what goes in the Coverage table's SDK pin
  column. For dict-form pins it is a short human-readable string (`tag v3.7.0`,
  `branch release/v2`, `rev 91e9c4a9`, `editable ../application-sdk`); for string-form
  pins it is the verbatim pin string.

Apply the gate decisions from Phase 0b after calling `compatible_majors`:

- **Set contains `target_major`** → proceed to Step 2.
- **Set is non-empty but excludes `target_major`** → status `skipped-major-mismatch`.
  Record `pin_display` and the compatible-majors set. **Stop.**
- **`None` + dict-form pin** → status `skipped-pin-source-unknown`. Record `pin_display`
  and `?` for compatible majors. **Stop.**
- **`None` + string-form pin** → status `analyzed-pin-unknown`. Proceed conservatively.
- **Both API calls fail** → status `analyzed-error` with the error message. Never
  silently drop.

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

Record every hit as an extended record (implemented in the same Python helper that parses `rg` output — read the file once, slice around each match):

```
(file_path, line_number, matched_line, check_id,
 context_before,      # 3 source lines preceding the match
 context_after,       # 3 source lines following the match
 enclosing_symbol,    # nearest def/class header above the match, if any
 try_body_excerpt)    # for `except …` patterns: the lines between `try:` and the matched
                      # `except` (best-effort); empty string otherwise
```

For hits where `require_anchor: false`, run the patterns against all Python files in the repo, not just
anchored ones (requires a clone or a separate `gh search code` call per pattern).

### Step 4b — Triage each finding

For each captured hit, assign a `triage` field and a one-line `triage_reason`:

| Value | Meaning |
|---|---|
| `confirmed`      | The hit represents real exposure to the SDK change being audited. |
| `false_positive` | The pattern matched, but the code is not exposed to the SDK change (see heuristics). |
| `needs_review`   | Insufficient signal in the captured context — flag for human review. |

#### Default heuristics (conservative — when in doubt, return `needs_review`)

**For `except <SpecificException>` patterns (definite-break checks):**
- `false_positive` if `try_body_excerpt` contains none of the spec's anchor patterns AND
  contains at least one of: `urllib`, `urlparse`, `os.path`, `int(`, `float(`,
  `json.loads`, `CredentialRef`, `re.compile`, `datetime.`, `uuid.`, `Decimal(`,
  `pathlib.`, `yaml.safe_load`, `Enum(`. These are standard sources of
  `ValueError`/`KeyError` unrelated to the SDK.
- `confirmed` if `try_body_excerpt` contains an anchor pattern (e.g. the class/symbol
  named in the spec) or a method call on a variable typed as the SDK class.
- `needs_review` otherwise.

**For literal message-string patterns (silent-break checks):**
- `false_positive` if the match is inside a `"""..."""` docstring, a `#` comment, or a
  regex literal assigned to a variable whose surrounding code does not invoke `str(e)`,
  `e.args[0]`, `.message`, `re.search`, `re.match`, or string `in` against an exception.
- `confirmed` otherwise — message-string inspection rarely produces FPs.

**For broad-catch patterns (review checks):**
- `false_positive` if `try_body_excerpt` contains only the consumer's own `raise <X>(...)`
  statement (catching to add context to a self-raise, not to inspect an SDK error).
- `confirmed` if `try_body_excerpt` calls an SDK anchor method.
- `needs_review` otherwise — broad catches are intrinsically ambiguous.

#### Spec-supplied heuristics (optional override)

A check spec MAY include a `triage:` block for project-specific FP signals. Spec heuristics
are evaluated **before** the defaults; defaults only fire when the spec rules are silent.

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

#### Surfacing the triage outcome

- Per-repo findings (Phase C) MUST list every hit, annotated inline:
  `[confirmed]`, `[false_positive — <reason>]`, or `[needs_review — <reason>]`.
- `false_positive` hits do NOT count toward the action summary or the executive-summary
  "Repos needing action" metric — but they are still listed so reviewers can challenge
  a triage call without re-running the audit.

### Step 5 — Classify status

| Status | Condition |
|---|---|
| `analyzed-no-usage` | major-match; no anchor match anywhere in the repo |
| `analyzed-no-findings` | major-match; anchor present, zero check hits — OR all hits classified `false_positive` (note `(N hits, all FPs)` in the coverage table) |
| `analyzed-has-findings` | major-match; at least one `confirmed` or `needs_review` check hit after Step 4b triage |
| `skipped-major-mismatch` | pin parsed; compatible majors do not include `target_major` |
| `skipped-pin-source-unknown` | `[tool.uv.sources]` pins to a rev hash or non-`release/vN`/non-`main` branch; major undetermined — skip analysis, record the rev/branch verbatim |
| `analyzed-pin-unknown` | string-form pin missing or unparseable; analyzed conservatively |
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
| Skipped (uv source pin — major undetermined) | N |
| Pin unknown (analyzed conservatively) | N |
| Repos with anchor-matched files | N |
| Repos needing action (after FP exclusion) | N |
| False-positive hits filtered out | N across M repos |
| <check_id> confirmed hits (<impact>) | N across M repos |
| … | … |

<2–3 sentence narrative: what matters, what's safe, what needs action. Note the FP filter
ratio if non-trivial (e.g. ">50% of R1 hits were false positives — narrow `except ValueError`
on non-SDK code such as URL parsing"). Include a note like: "Skipped N repos pinned to other
majors — those will be re-audited when they migrate to v<target_major>." When the "uv source
pin — major undetermined" count is non-zero, note: "N repos pinned via `[tool.uv.sources]`
to a specific rev or non-`release/vX` branch — major version cannot be derived without
checking out the pinned ref, so they are listed in Coverage with their rev/branch and
excluded from analysis.">

---

## Spec used

<Verbatim copy of the check spec from Phase 0, so the report is self-contained.>

---

## Coverage — every repo we looked at

Every candidate repo from Phase A is listed here regardless of outcome. Repos are sorted
alphabetically. `analyzed-error` rows include the error so follow-up is possible.

**Column notes:**
- **SDK pin** — for `[project].dependencies` / `requirements.txt` entries, the verbatim
  version specifier (e.g. `>=3.0.0,<4.0.0`). For `[tool.uv.sources]` entries, the
  `pin_display` string from `compatible_majors()` (e.g. `tag v3.7.0`, `branch release/v2`,
  `rev 91e9c4a9`, `editable ../application-sdk`). This lets a reviewer re-derive the major
  decision without re-fetching the file.
- **Compatible majors** — the set derived from the pin. `?` means major undetermined
  (`skipped-pin-source-unknown` or `analyzed-pin-unknown`).

| Repo | SDK pin | Compatible majors | Status |
|---|---|---|---|
| atlanhq/<repo-a> | `>=3.0.0,<4.0.0`  | {3} | analyzed-has-findings |
| atlanhq/<repo-b> | `2.7.4`           | {2} | skipped-major-mismatch |
| atlanhq/<repo-c> | `branch release/v2` | {2} | skipped-major-mismatch |
| atlanhq/<repo-d> | `rev 91e9c4a9`    | ?   | skipped-pin-source-unknown |
| atlanhq/<repo-e> | `tag v3.7.0`      | {3} | analyzed-no-usage |
| atlanhq/<repo-f> | (none)            | ?   | analyzed-pin-unknown |
| atlanhq/<repo-g> | —                 | —   | analyzed-error: 403 from gh api |

---

## Action summary — repos that need a migration PR

Only repos with at least one `confirmed` or `needs_review` finding appear here.
False-positive-only repos are excluded (see Coverage table for full traceability).
Sorted by priority descending, then by repo name.

| Repo | SDK pin | Definite breaks | Silent breaks | Review items | Needs review | Fix sites | Priority | Recommended action |
|---|---|---|---|---|---|---|---|---|
| atlanhq/<repo-a> | `>=3.0.0,<4.0.0` | 3 | 1 | 0 | 0 | `clients/foo.py:42`, `clients/foo.py:108`, `handlers/bar.py:17` (+1 more) | P0 | Catch `AuthError` instead of `ClientError`; replace `SQL_CLIENT_AUTH_ERROR` string match with `isinstance(e, SqlClientAuthFailedError)`. |
| atlanhq/<repo-b> | `>=3.5.0,<4`     | 0 | 0 | 2 | 1 | `app/init.py:88`, `app/init.py:140`, `tests/test_conn.py:24` | P2 | Review broad `except Exception` blocks near SDK calls; confirm whether they re-raise legacy types. |

**Column definitions:**
- **Definite breaks / Silent breaks / Review items** — count of `confirmed` findings at each impact level.
- **Needs review** — count of `needs_review` findings (any impact level).
- **Fix sites** — file:line list, capped at 6; if more, append `(+N more)`.
- **Priority** — P0: ≥1 confirmed definite-break. P1: ≥1 confirmed silent-break, 0 definite-breaks. P2: review-only or `needs_review`-only.
- **Recommended action** — the `recommendation:` lines from each check that produced a confirmed hit, deduplicated and joined with `; `.

---

## Per-repo findings (only repos with hits)

### atlanhq/<repo-name>

- **SDK pin:** `<version>`
- **Anchor files:**
  - `<path/to/file.py>` (line N: `<matched line>`)

#### <check_id> — <check name> (impact: <impact>)

- `<file>:<line>` — `<matched line>` [confirmed]
  - Recommendation: <from spec>
- `<file>:<line>` — `<matched line>` [false_positive — try body calls only urllib.parse.urlparse]
- `<file>:<line>` — `<matched line>` [needs_review — try body context ambiguous]

---

## Repos added from connector-map cross-check

<List any repos that were added from the connector-map but were absent from gh search results,
with the gap warning. Omit this section if there were no gaps.>
```

### Inline summary in chat

After writing the report, print to chat:

1. The executive summary table.
2. The Action summary table (only confirmed/needs-review repos).
3. The top 3–5 highest-impact **confirmed** hits (definite-break first, then silent-break,
   then review), with file:line and recommendation inline.
4. The report file path.

---

## Phase D — Optional verification (on request)

These steps are offered but NOT run automatically. Invoke them by asking:
`"verify the top findings"` or `"show me the GitHub links"`.

### D1 — Spot-check via GitHub URLs

For each `needs_review` finding, and for a sample of `false_positive` findings (to verify
the triage logic is correct), construct the direct link:
```
https://github.com/atlanhq/<repo>/blob/<default-branch>/<path>#L<line>
```
Open in browser or present as a list. Lets the reviewer confirm the triage classification
is accurate — e.g. that a `false_positive` tagged hit truly does not call an SDK method,
or that a `needs_review` hit warrants confirmation/dismissal.

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
