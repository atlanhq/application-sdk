---
name: audit-consumers
description: >
  Audit every github.com/atlanhq/ consumer of application_sdk against a user-supplied check
  specification. Discovers consumer repos via `gh search code`, applies one or more named
  checks per repo (grep patterns anchored to SDK symbol usage), and produces a markdown
  report — including a coverage table listing every analyzed repo with its outcome
  (no-usage / no-findings / has-findings). Optionally raises migration PRs against consumer
  repos with confirmed findings. Use when proposing an SDK breaking change, removing a
  deprecated symbol, or proactively measuring the blast radius of a refactor before merging.
argument-hint: "[--spec <path>] [--report <path>] [--sdk-major <N>] [--sdk-release <ref>] [--raise-prs] [--dry-run]"
mandatory_triggers:
  - "/audit-consumers"
  - "audit application_sdk consumers"
  - "audit SDK consumers"
  - "check consumers of application_sdk"
optional_triggers:
  - "blast radius of this SDK change"
  - "who uses this SDK symbol"
  - "which apps would break if"
  - "raise migration PRs"
  - "auto-fix consumers"
owner: connector-platform-team
last_updated: "2026-05-15"
staleness_days: 90
inputs:
  - spec: "path to a check-spec markdown file, OR inline natural-language description from the invoker"
  - report: "output path for the report markdown (default: <repo-root>/<slug>-consumer-audit.md)"
  - sdk-major: "integer target major version; repos on other majors are skipped"
  - sdk-release: "the application_sdk release/PR/SHA this audit targets (e.g. v4.0.0, application_sdk#1234). If absent, Phase 0 prompts for it."
  - raise-prs: "optional flag — after Phase C, clone each analyzed-has-findings repo, apply fixes, verify, and open a ready-for-review PR. If absent and findings exist, the user is prompted interactively after Phase C."
  - dry-run: "optional flag (with --raise-prs) — run Phase E through E6 (generate + verify) but stop before push and PR creation"
outputs:
  - markdown report at the specified path
  - inline executive summary (top findings) in chat
  - "when PR-raising is enabled: per-repo PR URLs in chat, plus a final summary table"
gates:
  - "Consumer repos are read-only unless --raise-prs is set (or the user accepts the post-Phase-C prompt). When PR-raising is enabled, fixes are applied on branch sdk-audit/<spec-slug> and pushed to open a ready-for-review PR — never to main, never force-pushed."
  - "Coverage table must list every candidate repo from Phase A, even when no findings."
  - "The only file written in the SDK repo is the report — no edits to application_sdk source."
  - "When PR-raising is enabled, only confirmed findings drive auto-fixes. needs_review findings become PR-body TODOs. false_positive findings are skipped."
  - "Commits MUST follow Conventional Commits and MUST NOT include Co-Authored-By lines (per application-sdk CLAUDE.md). PR body MAY include the 🤖 Claude Code footer."
  - "Never include customer/tenant/run-ID strings in commit messages, code, or PR bodies (per application-sdk CLAUDE.md)."
references:
  - references/spec-format.md    # check-spec format, fix: block syntax, triage overrides
  - references/fix-generation.md # E4a deterministic + E4b LLM-fallback + diff rejection rules
  - references/pr-templates.md   # PR body template, commit message template, branch naming
  - references/failure-modes.md  # failure table, manifest.json schema, end-of-phase summary
  - references/examples.md       # worked check-spec examples (with and without fix: blocks)
---

# Skill: `/audit-consumers`

Discovers every `github.com/atlanhq/` consumer of `application_sdk`, applies a user-defined
check specification across each consumer, and produces a markdown report that accounts for
every candidate repo — including those with no relevant usage — so the audit is demonstrably
holistic.

Optionally raises ready-for-review migration PRs against each consumer with confirmed findings,
with fixes applied (deterministically from a `fix:` block in the spec, or LLM-generated from
the prose `recommendation:`).

**Typical use cases:**
- "Which apps still import deprecated symbol `X` that we are removing in v4.0?"
- "Who catches exceptions from `BaseSQLClient.load()` narrowly, by class or message string?"
- "Which consumers pin the SDK to a version before feature `Y` shipped?"
- "What is the blast radius if we change the signature of `ObjectStore.upload_file()`?"
- "Raise migration PRs against all consumers that will break when we ship PR #1234."

---

## Invocation

```
/audit-consumers                                  # prompts for a spec inline
/audit-consumers --spec ./my-spec.md             # reads spec from file
/audit-consumers --report ./out.md               # custom report path
/audit-consumers --sdk-major 3                   # explicitly set the target major version
/audit-consumers --sdk-release v4.0.0            # target SDK release for PR bodies
/audit-consumers --raise-prs                     # run Phase E and open PRs after Phase C
/audit-consumers --raise-prs --dry-run           # Phase E through E6 only — no push, no PR
```

Argument parsing (left-to-right, first match wins):
- `--spec <path>` — read check specification from this file instead of prompting
- `--report <path>` — write the markdown report here; default: `<repo-root>/<slug>-consumer-audit.md`
  where `<slug>` is the spec title lowercased, spaces replaced with hyphens
- `--sdk-major <N>` — integer target major version to audit against; repos pinned to other
  majors are recorded as `skipped-major-mismatch` without file analysis (see Phase 0b)
- `--sdk-release <ref>` — free-form string naming the SDK release (version tag, PR URL, or
  commit SHA); used in the report and PR bodies; if absent, Phase 0 prompts for it
- `--raise-prs` — after Phase C, enter Phase E (generate and raise migration PRs)
- `--dry-run` — used with `--raise-prs`; runs through Phase E6 but does not push or create PRs

---

## Phase 0 — Capture the check specification

**If `--spec <path>` was provided:** read the file. Validate that it contains at least one
anchor pattern and at least one named check. If malformed, report the exact problem and stop.

**Otherwise:** ask the user to describe what they want to check. Convert the description into a
structured spec by echoing it back in the format below for confirmation.

> **Gate:** confirm the spec with the user before running any GitHub queries. This is the
> **only** interactive step before Phase C — once the spec is locked, Phases A–C run to
> completion without further prompts.

The check-spec format is defined in `references/spec-format.md`. The short form for Phase 0
confirmation:

```markdown
# Check spec — <Short title (becomes the report filename slug)>

## Context
<Why this audit exists and what SDK change prompted it.>

## target_release
<Version tag, PR number, or "pre-release". Prompted here if --sdk-release was not passed.>

## Anchors
- pattern: `<regex>`

## Checks
### check: R1 — <Name>
- impact: definite-break | silent-break | review
- patterns:
  - `<regex>`
- recommendation: <one-line fix>
- fix:           # optional — see references/spec-format.md
  - pattern_index: 0
    replace_regex: `…`
  add_import: `from …`
```

**Additional validation when `--raise-prs` is set:**
Every `fix:` block is validated before Phase A runs. See `references/spec-format.md` for
the validation rules. Failures are reported with the exact check ID and field; stop before
Phase A.

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
    m = re.match(r"release/v(\d+)", pin)
    if m:
        return {int(m.group(1))}, display
    m = re.match(r"v(\d+)\.", pin)
    if m:
        return {int(m.group(1))}, display
    if pin.startswith("rev:"):
        return None, display
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
            return {lower}, display
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
        line = line.split("#", 1)[0].strip()
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
            break

# --- Stage 2: [project].dependencies or requirements.txt (fallback) ---
project_pin = None
if uv_source is None and text:
    m = re.search(
        r'["\']atlan[-_]application[-_]sdk[^"\']*["\']', text, re.IGNORECASE
    )
    if m:
        ver = re.search(r'((?:[><=!~^,\s\d\.\*]+)+)', m.group().split("[")[0].split('"')[-1])
        project_pin = ver.group(1).strip() if ver else None

if uv_source is None and project_pin is None and not text:
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
  found in the sources table, use it exclusively.
- Match the SDK package under either `atlan-application-sdk` or `application-sdk`
  (case-insensitive, hyphen or underscore).
- Commented-out lines are stripped before matching — only the active, uncommented source is used.
- `pin_display` is what goes in the Coverage table's SDK pin column.

Apply the gate decisions from Phase 0b after calling `compatible_majors`.

### Step 2 — Anchor check (cheap, via GitHub API)

```bash
gh search code "<anchor>" --repo atlanhq/<repo> --json path,textMatches --limit 20
```

Run this for each anchor pattern in the spec. If all return empty → status `analyzed-no-usage`.
If `gh api` fails → status `analyzed-error`. Never silently drop.

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

Record every hit as an extended record:

```
(file_path, line_number, matched_line, check_id,
 context_before,      # 3 source lines preceding the match
 context_after,       # 3 source lines following the match
 enclosing_symbol,    # nearest def/class header above the match, if any
 try_body_excerpt)    # for `except …` patterns: lines between `try:` and the matched
                      # `except` (best-effort); empty string otherwise
```

For hits where `require_anchor: false`, run the patterns against all Python files in the repo.

### Step 4b — Triage each finding

For each captured hit, assign a `triage` field and a one-line `triage_reason`:

| Value | Meaning |
|---|---|
| `confirmed`      | Real exposure to the SDK change being audited. |
| `false_positive` | Pattern matched, but code is not exposed to the SDK change. |
| `needs_review`   | Insufficient signal — flag for human review. |

#### Default heuristics (conservative — when in doubt, return `needs_review`)

**For `except <SpecificException>` patterns (definite-break checks):**
- `false_positive` if `try_body_excerpt` contains none of the spec's anchor patterns AND
  contains at least one of: `urllib`, `urlparse`, `os.path`, `int(`, `float(`,
  `json.loads`, `CredentialRef`, `re.compile`, `datetime.`, `uuid.`, `Decimal(`,
  `pathlib.`, `yaml.safe_load`, `Enum(`.
- `confirmed` if `try_body_excerpt` contains an anchor pattern or a method call on a
  variable typed as the SDK class.
- `needs_review` otherwise.

**For literal message-string patterns (silent-break checks):**
- `false_positive` if the match is inside a `"""..."""` docstring, a `#` comment, or a
  regex literal whose surrounding code does not invoke `str(e)`, `e.args[0]`, `.message`,
  `re.search`, `re.match`, or string `in` against an exception.
- `confirmed` otherwise.

**For broad-catch patterns (review checks):**
- `false_positive` if `try_body_excerpt` contains only the consumer's own `raise <X>(...)`.
- `confirmed` if `try_body_excerpt` calls an SDK anchor method.
- `needs_review` otherwise.

Spec-supplied `triage:` overrides are evaluated before these defaults. See `references/spec-format.md`.

#### Surfacing the triage outcome

- Per-repo findings (Phase C) MUST list every hit, annotated inline:
  `[confirmed]`, `[false_positive — <reason>]`, or `[needs_review — <reason>]`.
- `false_positive` hits do NOT count toward the action summary metrics — but are listed
  so reviewers can challenge a triage call.

### Step 5 — Classify status

| Status | Condition |
|---|---|
| `analyzed-no-usage` | major-match; no anchor match anywhere in the repo |
| `analyzed-no-findings` | major-match; anchor present, zero check hits — OR all hits `false_positive` |
| `analyzed-has-findings` | major-match; at least one `confirmed` or `needs_review` hit after triage |
| `skipped-major-mismatch` | pin parsed; compatible majors do not include `target_major` |
| `skipped-pin-source-unknown` | `[tool.uv.sources]` pins to rev hash or non-`release/vN`/non-`main` branch |
| `analyzed-pin-unknown` | string-form pin missing or unparseable; analyzed conservatively |
| `analyzed-error` | GitHub API call failed (log the error) |

---

## Phase C — Synthesise and write the report

Write the report to the path specified by `--report` (or the default slug-based path).

### Required report sections

```markdown
# <Spec title> — Consumer Audit

**Audit date:** <ISO date>
**Target SDK release:** <spec.target_release>
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

<2–3 sentence narrative.>

---

## Spec used

<Verbatim copy of the check spec from Phase 0, so the report is self-contained.>

---

## Coverage — every repo we looked at

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

| Repo | SDK pin | Definite breaks | Silent breaks | Review items | Needs review | Fix sites | Priority | Recommended action |
|---|---|---|---|---|---|---|---|---|
| atlanhq/<repo-a> | `>=3.0.0,<4.0.0` | 3 | 1 | 0 | 0 | `clients/foo.py:42`, … | P0 | Catch `AppError` instead of `ClientError`; … |

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

<List any repos added from the connector-map but absent from gh search results.
Omit this section if there were no gaps.>
```

### Post-Phase-C interactive prompt (when `--raise-prs` was not set)

After writing the report and printing the inline chat summary, if ALL of the following hold:
- `--raise-prs` was **not** set at invocation
- `--dry-run` was **not** set
- At least one repo has status `analyzed-has-findings` with at least one `confirmed` hit

Then prompt:

> "Audit found <N> repos with confirmed findings. Raise migration PRs against these
> consumers now? (yes/no)"

A `yes` answer enters Phase E exactly as if `--raise-prs` had been set at invocation.
A `no` answer continues to Phase D (or exits).

### Inline summary in chat

After writing the report, always print to chat:

1. The executive summary table.
2. The Action summary table (only confirmed/needs-review repos).
3. The top 3–5 highest-impact **confirmed** hits (definite-break first, then silent-break,
   then review), with file:line and recommendation inline.
4. The report file path.

---

## Phase E — Generate and raise migration PRs

Runs after Phase C when `--raise-prs` is set or the user accepts the post-Phase-C prompt.
Only operates on repos with status `analyzed-has-findings` and at least one `confirmed` hit.
`needs_review` findings become PR-body TODOs; `false_positive` findings are skipped.
`analyzed-pin-unknown`, `skipped-major-mismatch`, and `skipped-pin-source-unknown` repos
are excluded entirely.

For `fix:` block syntax see `references/spec-format.md`.
For LLM prompt template and diff rejection rules see `references/fix-generation.md`.
For PR body, commit message, and branch naming see `references/pr-templates.md`.
For failure modes, `manifest.json` schema, and end-of-phase summary see `references/failure-modes.md`.

### E0 — Pre-flight

1. Run `gh auth status`. If it fails: print the error, instruct `gh auth login --scopes repo`,
   exit Phase E (report is already written). Do not proceed.
2. Compute the branch slug from the spec title (lowercase, spaces → hyphens, trim to ≤30 chars).
   Branch name = `sdk-audit/<spec-slug>`.
3. Build the target repo list: filter Phase B results to `analyzed-has-findings` + ≥1
   `confirmed` hit; sort by Action Summary priority (P0→P2), then alphabetically.
4. Create `/tmp/audit-fix/<spec-slug>/` if it doesn't exist. If `manifest.json` already exists,
   load it and merge: preserve all repo entries already in terminal states (`pr_open`, `skipped`,
   `failed`) and carry forward `auto_confirm_remaining`; reset any `pending` entries for retry.
   Write the merged manifest back. This enables crash recovery — re-running resumes from where
   the interrupted run left off.
5. If `--dry-run`: print `[Phase E] DRY-RUN MODE — generating and verifying fixes, but no push and no PR.`
6. If all checks have `impact: review` only: warn and ask `"Continue anyway? (yes/no)"`.
7. Print: `[Phase E] Pre-flight OK · Branch: sdk-audit/<spec-slug> · Repos: N (P0: X, P1: Y, P2: Z)`

### E1 — Per-repo write-access check

```bash
gh api repos/atlanhq/<repo> --jq '{archived, push: .permissions.push, default_branch}'
```

- `archived: true` → state `skipped`, reason `repo archived`
- `push: false` → state `skipped`, reason `no push permission for <gh-user>`
- Otherwise: stash `default_branch` for use as the PR base; proceed to E2

### E2 — Per-repo idempotency check

```bash
gh pr list --repo atlanhq/<repo> --head sdk-audit/<spec-slug> --state open --json number,url
```

- No results → proceed to E3
- Open PR found → state `pr_open`, `pr_url=<existing>`. Print: `[Phase E] atlanhq/<repo>: PR already open: <url> — skipping`

### E3 — Per-repo clone and prepare branch

```bash
mkdir -p /tmp/audit-fix/<spec-slug>/<repo>
git clone --depth 50 git@github.com:atlanhq/<repo>.git /tmp/audit-fix/<spec-slug>/<repo>
cd /tmp/audit-fix/<spec-slug>/<repo>
git checkout -b sdk-audit/<spec-slug> origin/<default_branch>
```

Depth 50 (not 1) — cheap on disk; gives reviewers blame context. If the local directory
already exists (prior interrupted run), delete and re-clone — E2 already gates the remote.

### E4 — Per-repo generate fixes

For each confirmed hit, grouped by file, processed in **descending line order** (so byte
offsets stay valid as lines are modified):

- If the hit's check has a matching `fix:` entry → **E4a deterministic path**
- Otherwise → **E4b LLM fallback path**

After all hits in a file are processed, apply `add_import(s)` for any check that produced
at least one applied fix in that file.

Full algorithm detail: `references/fix-generation.md`.

### E5 — Per-repo verify

```bash
CHANGED=$(git diff --name-only --diff-filter=AM origin/<default_branch>... -- '*.py')
python -m py_compile $CHANGED
if command -v ruff >/dev/null; then ruff check $CHANGED; else echo "ruff: not installed, skipped"; fi
```

- `py_compile` fails → state `failed`; roll back the patched files
  (`git checkout origin/<default_branch> -- <file>` for each); do NOT proceed to E6/E7.
- `ruff check` fails → proceed; capture verbatim output for the PR body Verification block.

### E6 — Per-repo diff preview and confirmation

Print:
- Files changed + per-file hit counts (patched / unresolved)
- Verification status (py_compile + ruff)
- List of unresolved hits (those that will appear as PR-body TODOs)
- `git diff --stat` followed by `git diff` (capped at 200 lines)

If `--dry-run`: print `[Phase E] DRY-RUN — stopping here. No push, no PR.` and move to the
next repo.

Otherwise prompt:
```
Open a PR for atlanhq/<repo>?  (yes / skip / all)
  yes  — open the PR now (ready for review)
  skip — discard the branch, move on to the next repo
  all  — open this and auto-confirm every remaining repo in this session
```

Accepted inputs: `yes|y|skip|s|all|a`. On `all`: persist `auto_confirm_remaining: true` in
`manifest.json` and skip the prompt for subsequent repos. On `skip`: discard the local clone,
record state `skipped`.

### E7 — Per-repo commit, push, open PR

```bash
git add -A
git commit -m "$(cat <<'EOF'
<commit message — see references/pr-templates.md>
EOF
)"

git push -u origin sdk-audit/<spec-slug>

gh pr create \
  --repo atlanhq/<repo> \
  --base <default_branch> \
  --head sdk-audit/<spec-slug> \
  --title "<commit message subject line>" \
  --label "sdk-migration" \
  --body "$(cat <<'EOF'
<PR body — see references/pr-templates.md>
EOF
)"
```

The `sdk-migration` label is applied if it exists; `--label` failure is ignored (do not
try to create it). Print: `[Phase E] atlanhq/<repo>: PR opened → <url>`

Update `manifest.json` with state `pr_open` and `pr_url`.

After all repos: print the end-of-phase summary from `references/failure-modes.md`.

---

## Phase D — Optional verification (on request)

These steps are offered but NOT run automatically. Invoke them by asking:
`"verify the top findings"` or `"show me the GitHub links"`.

### D1 — Spot-check via GitHub URLs

For each `needs_review` finding, and for a sample of `false_positive` findings, construct
the direct link:
```
https://github.com/atlanhq/<repo>/blob/<default-branch>/<path>#L<line>
```

### D2 — Behaviour simulation

For checks with `impact: silent-break`, demonstrate that the old match would fail against a
newly typed error:

```python
from application_sdk.errors.<module> import <NewErrorType>
e = <NewErrorType>(...)
assert "<old_fragment>" not in str(e), f"Still matches: {str(e)!r}"
```

Only run when explicitly requested.

---

## Appendix

For worked check-spec examples (with and without `fix:` blocks), see `references/examples.md`.
