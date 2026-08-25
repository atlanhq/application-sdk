---
name: review-connector-pr
description: >
  Senior connector-reliability review for a diff/PR that ships to the app fleet
  (80+ production connectors running on Temporal). Reviews strictly for
  regression risk, runtime/cost increase at scale, and production failure or
  silent data loss — not style, naming, or generic best practices. Produces a
  severity-ranked verdict (SHIP / SHIP WITH FIXES / DON'T SHIP), a triage
  table, full-detail P0/P1 findings with file:line evidence, pre-merge checks,
  new-test coverage gaps, and a machine-readable JSON block. Use when reviewing
  a PR against application_sdk itself, a shared base class, or any individual
  connector app, especially before it rolls out across tenants.
argument-hint: "[--pr <url|number> | --diff <path>] [--connectors <desc>] [--runs-on <stage>] [--scale <N assets, M calls>] [--deploy <shape>]"
mandatory_triggers:
  - "/review-connector-pr"
  - "review this connector PR"
  - "review this app fleet PR"
  - "connector reliability review"
optional_triggers:
  - "will this break other connectors"
  - "blast radius of this SDK PR"
  - "is this safe to ship to all tenants"
owner: connector-platform-team
last_updated: "2026-08-24"
staleness_days: 120
inputs:
  - name: pr
    description: "PR URL or number (e.g. `atlanhq/application-sdk#1234`). If given, fetch via `gh pr diff <n>` / `gh pr view <n>`. Mutually exclusive with --diff."
    required: false
  - name: diff
    description: "Path to a local diff/patch file, or a git ref range (e.g. `main...HEAD`) to run `git diff` against. Use when there is no PR yet."
    required: false
  - name: connectors
    description: >
      Which connectors are affected — e.g. "all SQL connectors via SDK base
      class" or "Confluence only". Load-bearing: without it, blast-radius
      findings degrade into hedging ("might affect some connectors"). If
      omitted, infer it by grepping the repo for consumers of the changed
      symbols/files before writing findings, and state that inference
      explicitly rather than skipping the field.
    required: false
  - name: runs-on
    description: "Where in the pipeline this executes: Temporal workflow | activity | preflight | extraction | transform | publish. Affects which Temporal-specific checks apply (heartbeats, history size, replay safety)."
    required: false
    default: "infer from the diff (workflow/activity decorators, file location under app code)"
  - name: scale
    description: >
      Scale reference: largest tenant's asset count and per-crawl source API
      call count (e.g. "~500k assets, ~2M API calls"). Load-bearing for
      Category 2 findings — runtime/cost findings must quantify a multiplier
      or call-count delta against this number, not say "may be slower."
    required: false
    default: "ask the user if a runtime/cost-sensitive change is found and no scale reference was given"
  - name: deploy
    description: "Deployment shape: rolling across all tenants | single tenant pilot | new connector only. Affects blast-radius severity weighting."
    required: false
    default: "rolling across all tenants (worst case, if unstated)"
outputs:
  - "inline chat report following the fixed Output format below (verdict, triage table, full findings, pre-merge checks, coverage gaps, JSON block)"
gates:
  - "Read-only review. Never edit, commit, or push anything as part of this skill — findings only."
  - "Never propose modifying or deleting existing tests. New tests only, as scenario + assertion."
  - "Every finding cites file:line and quotes the relevant lines. No unsourced claims."
  - "Silent partial data loss always outranks a loud crash in severity — do not soften a P0 to P1 out of uncertainty; lower Confidence instead."
  - "Do not include real customer/tenant names or run IDs in the report (per application-sdk CLAUDE.md); use generic placeholders."
---

# Skill: `/review-connector-pr`

Runs a strict, three-category reliability review of a diff that ships to the
application-sdk connector fleet, and returns a fixed-format report ending in
a machine-readable JSON block. This skill is a lens, not a general code
reviewer — skip style, naming, formatting, and generic best-practice comments
entirely; assume the author is competent.

## Invocation

```
/review-connector-pr --pr 1234
/review-connector-pr --diff main...HEAD --connectors "all SQL connectors via SDK base class" --scale "~500k assets, ~2M calls"
/review-connector-pr --pr atlanhq/application-sdk#1234 --runs-on activity --deploy "rolling across all tenants"
```

## Step 0 — Resolve inputs

1. **Get the diff.**
   - `--pr <n>`: `gh pr diff <n>` for the diff, `gh pr view <n> --json title,body,baseRefName,headRefName` for context.
   - `--diff <path>`: read the file directly.
   - `--diff <ref-range>` (e.g. `main...HEAD`): `git diff <range>`.
   - Neither given: ask which PR/ref to review before doing anything else.

2. **Resolve Affected connectors** (load-bearing — do not skip).
   - If `--connectors` was given, use it verbatim as the reviewer's frame, but still verify it against the diff (see Step 1).
   - If omitted: identify every changed symbol (function, class, config key, base-class method) and grep the repo for its call sites / subclasses / consumers. State the resulting connector list explicitly in the report's opening line, e.g. "Affected (inferred): all connectors subclassing `BaseSQLClient` — grep found 14 connector packages importing it." Do not silently proceed with an unstated scope.

3. **Resolve Runs-on, Scale, Deploy shape** per the `inputs` defaults above. If a Category 2 (runtime/cost) concern surfaces and no `--scale` was given, ask the user for the largest tenant's asset/API-call scale before finalizing that finding's severity — a runtime finding without a quantified scale reference is not acceptable output.

## Step 1 — Read the diff for real, not just the patch

For every changed file:
- Read enough of the **surrounding unchanged code** (not just diff context lines) to know the real call sites, loop structure, and error-handling around the change.
- Search the repo for **every** call site of any changed function/class signature or return shape — do not assume the diff's own test file shows them all.
- If the change touches a shared/base-class/SDK-level path, identify every connector that inherits or imports that path, and separately identify which of those are exercised by this PR's tests (via `git diff` test files) versus not. Name the untested ones explicitly.

## Step 2 — Apply the three review categories

Work through each category below. These are the *only* categories in scope.

### Category 1 — Regression risk (behaviour change not obvious from the diff)
- Changed defaults, signatures, or return shapes — enumerate every call site found in Step 1.
- Shared/base-class/SDK-level changes — name every inheriting connector NOT exercised by this PR's tests.
- Config/manifest/env-var changes that already-deployed tenants won't have set — what happens when the key is absent?
- Serialization/schema changes crossing a Temporal boundary (workflow ↔ activity, or replayed history) — will in-flight workflows started on old code break on replay?
- Error handling narrowed or broadened — exceptions now swallowed, retried, or newly raised.
- Idempotency — does a retry of this path now double-write, double-count, or skip?
- Ordering assumptions on unordered structures (dict/set/query results).

### Category 2 — Runtime and cost increase (quantify against the Scale reference — no hedging)
- New work inside a per-asset/per-row loop: N+1 API/DB calls, per-item auth, per-item regex compile, per-item client/logger construction.
- Reduced batch/page size or concurrency; new lock/semaphore/await that serializes previously parallel work.
- New network/DB call on a hot path; synchronous work inside an async path that can block the event loop.
- Unbounded memory: full result set materialized before writing, no streaming/chunking.
- Temporal-specific: activity likely to exceed start-to-close timeout; missing/less-frequent heartbeat; larger payloads inflating workflow history.
- **State a multiplier, an API-call count, or a wall-clock estimate at the resolved Scale reference.** "May be slower" is not a finding — if you cannot quantify it, say what additional info would let you, and ask for it rather than publishing a vague finding.

### Category 3 — Production failure or silent data loss (weight this hardest)
The worst outcome is a crawl that **succeeds but returns fewer assets than before.**
- Pagination: cursor/offset handling, terminating condition, dropped first/last page, page-size cap changes, cursor invalidation mid-crawl.
- Filtering: any new/moved `if`/`continue`/`WHERE` that could exclude valid assets — case sensitivity, null vs empty-string vs missing, unicode, trailing whitespace.
- `try/except` that logs and continues — does a partial failure now produce a green run with missing data? Is it counted, surfaced, and attributable to a specific asset?
- Deduplication/qualified-name/key-generation changes that could collide and overwrite.
- Permission/auth-scope assumptions — on a read-only or partial-grant tenant, does this hard-fail or silently return empty?
- Upstream API variance: 429s, pagination quirks, deprecated fields, empty vs null vs absent across source versions.
- Timezone or incremental-watermark changes that could skip a window.
- Deleted/soft-deleted asset handling — could this mark live assets as deleted?

## Step 3 — Assign severity

| Sev | Definition | Test |
|-----|------------|------|
| **P0 — Blocker** | Silent data loss, or hard failure across multiple connectors. Crawl goes green but assets are missing, overwritten, or wrongly marked deleted. | "Would a customer lose assets and not know?" |
| **P1 — High** | Hard failure on a specific connector/tenant shape, or a breaking change to a shared SDK/base-class contract. Loud, but ships broken. | "Will a real tenant's crawl error out?" |
| **P2 — Medium** | Runtime/cost regression at scale, timeout/heartbeat risk, unbounded memory. Correct results, unacceptable cost. | "Does this get worse as N grows?" |
| **P3 — Low** | Latent risk, missing guard, unverified assumption not triggered by current call sites. | "Is this a landmine for the next PR?" |

Rules:
- Silent partial data **always** outranks a loud crash — a stack trace is visible, missing rows are not.
- Blast radius promotes severity by one tier — the same defect in a shared SDK path outranks the same defect in a single connector.
- Never soften a P0 to P1 out of uncertainty. Keep the severity; lower `Confidence` instead.
- If a category has zero findings, say so explicitly. Do not manufacture findings to fill the template.
- Root cause, not symptom: if the diff patches over a deeper defect, name the real fix, not the band-aid.
- Mark `Verified: verified in code` vs `Verified: inferred from <what>` on every finding.
- Never propose modifying or deleting an existing test — new tests only.

## Step 4 — Emit the report (fixed format, follow exactly)

### 1. Verdict — always first

```
VERDICT:   SHIP | SHIP WITH FIXES | DON'T SHIP
REASON:    <one sentence — the single decisive finding>
COUNTS:    P0: n   P1: n   P2: n   P3: n
```

### 2. Triage table

One row per finding, ordered P0 → P3. Nothing else in this section.

| ID | Sev | Category | Location | One-line issue | Blast radius | Confidence |
|----|-----|----------|----------|----------------|--------------|------------|
| F1 | P0 | Data-loss | `sdk/paginate.py:88` | Cursor loop exits before final page | All 40 API connectors | High |

### 3. Findings — full detail, P0 first

P0 and P1 in full:

```
[F1] <Title>
Category:     Regression | Runtime | Data-loss
Location:     path/to/file.py:88-104
Evidence:     <quote the actual lines>
Mechanism:    <why it breaks, 1-2 sentences>
Blast radius: <connectors / tenant shapes / crawl stage>
Failure mode: hard error | silent partial data | slow crawl | cost
Trigger:      <exact condition that fires it>
Root cause:   <the real defect, not the symptom>
Fix:          <root-cause fix>
Verified:     verified in code | inferred from <what>
Confidence:   High | Medium | Low
```

P2 (abbreviated):

```
[F7] <Title> — path/to/file.py:210
Impact at scale: <quantified — "3 → 3+N calls; ~40k extra API calls at 40k assets">
Fix: <one line>
```

P3: `[F11] <one line> — path/to/file.py:55`

### 4. Pre-merge checks

Ordered, most critical first, each concretely executable: which connector, which tenant shape, which command; what to compare against (baseline asset count, prior run ID reference — generic, never a real run ID); what result proves the fix.

### 5. Coverage gaps

New tests that should exist, as scenario + assertion. Additions only.

### 6. Machine-readable block

```json
{
  "verdict": "ship | ship_with_fixes | dont_ship",
  "reason": "...",
  "counts": { "p0": 0, "p1": 0, "p2": 0, "p3": 0 },
  "findings": [
    {
      "id": "F1",
      "severity": "p0",
      "category": "data_loss",
      "file": "sdk/paginate.py",
      "lines": "88-104",
      "title": "...",
      "mechanism": "...",
      "blast_radius": ["..."],
      "failure_mode": "silent_partial_data",
      "trigger": "...",
      "root_cause": "...",
      "fix": "...",
      "verified": true,
      "confidence": "high"
    }
  ]
}
```

## Notes on this repo

- Confidentiality: never put real customer/tenant names or production run IDs in the report — the same rule applies here as to commits/PRs (see root `CLAUDE.md`). Use generic placeholders like `tenant_123`.
- If the diff touches `.github/workflows/**` or other security-relevant control-plane files, flag that explicitly in the verdict reason even if it's outside the three categories — those changes need separate human sign-off per org policy.
- This skill does not run tests or linters itself; it is a static reliability review. If you want test/lint results folded in, run them separately and paste the output for this skill to reason about.
