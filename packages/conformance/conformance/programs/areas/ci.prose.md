---
kind: responsibility
name: ci-area
description: >
  Maintains the current C-series violation-set.  C002 (and C003's
  absent-`.gitignore` case) are mechanically remediated by invoking
  `atlan-application-sdk-conformance bootstrap` — a deterministic re-sync, not
  a model-authored edit.  C001 is mechanically remediated by resolving the
  mutable ref to a commit SHA and rewriting the `uses:` line, then always
  routed to residue for human sign-off (the SHA comes from a live external
  lookup).  C003's missing-entry case and drifted `tests.yaml`/`renovate.json`
  still have no authored prescription and route to residue for manual triage.
---

### Maintains

The current set of unsuppressed C-series (CI / GitHub Actions) conformance
findings in the working tree, as reported by `suite.runner --series C`.

#### violations-ci

The fingerprint-set of all unsuppressed FAILING C-series results.  Extends to
include WARNING results in strict mode.

Postcondition:

> `atlan-application-sdk-conformance detect --repo . --series C` shows zero
> unsuppressed C002 findings (and zero C003 absent-`.gitignore` findings)
> after the `bootstrap` re-sync below, and zero unsuppressed C001 findings
> after every mutable ref is repinned to a resolved SHA — though every C001
> fix is additionally escalated to residue for human sign-off regardless of
> recheck passing (`external_influence`).  Any C-series findings that remain
> (C003 missing-entry, drifted `tests.yaml`/`renovate.json`) are not yet
> remediable in this phase and are routed to residue for manual triage — see
> the Fix Prescription for exactly which findings that covers and why.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when any file under `.github/` changes.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "C"
  mode: mode
  max_attempts: 5
```

`remediate-finding` dispatches C002 (and C003 absent-file findings) to the
mechanical `bootstrap` fix, and C001 to the SHA-resolution fix, both below.
Every other C-series finding returns `not_remediable = true` and is routed to
residue by the pattern itself. No special-casing is needed here beyond the
dispatch — `detect-fix-recheck`'s existing loop, recheck, and residue
machinery (including its `external_influence` → residue branch, which is
what routes every successful C001 fix to human review) apply unchanged.

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "ci"`._

**C002 BootstrapWorkflowDrift** — a managed CI file (workflow shim, vendored
action file, or scaffold) is absent or has drifted from what
`atlan-application-sdk-conformance bootstrap` would write. `classification =
"mechanical"` — the fix is a deterministic re-sync, not a model-authored
edit: `bootstrap` overwrites managed files with the exact canonical rendered
template the C002 checker itself renders for comparison, so there is nothing
for the model to invent or judge.

*Procedure* — run once per remediation pass; one invocation re-syncs every
managed file, so it clears every other pending C002 (and C003 absent-file)
finding in the same pass:

1. Preserve the two per-repo customizations `bootstrap` does **not**
   auto-detect from the repo, so re-running it doesn't reset them to
   defaults:
   - If `.github/workflows/docstring-coverage.yaml` exists, extract its
     `package_name: "<value>"` and pass `--package-name <value>`.
   - If `.github/workflows/build-and-publish.yaml` exists, extract its
     `unit_tests_workflow_file: "<value>"` and pass `--unit-tests-workflow
     <value>`.
   (`--app-name` and `--services-script` need no extraction — `bootstrap`
   auto-detects both itself, from `atlan.yaml`/the repo directory name and
   from an existing `.github/test/setup-services.sh` respectively.)
2. Run:
   ```
   atlan-application-sdk-conformance bootstrap \
     --package-name <extracted-or-default> \
     --unit-tests-workflow <extracted-or-default>
   ```
3. This single command resolves, in one pass: every absent/drifted managed
   workflow shim, every absent/drifted vendored action file, an absent
   `.gitignore` (C003's absent-file case — see below), an absent
   `renovate.json`, and an absent `tests.yaml`.
4. `outcome = "fix"`. `orthogonal_gate = "none"` on both C002 and C003 (set
   on the rule definitions) — the test suite gate is skipped entirely, not
   just for this fix: a `.github/`/scaffold-only change cannot affect Python
   behaviour. `recheck-narrowest` (re-running `suite.runner --series C`) is
   the only verification that applies.
5. Any C002 finding still present after this pass is genuinely not
   bootstrap-fixable and falls into the residue cases below.

*Not resolved by this procedure — route to residue:*

- **`tests.yaml` drift** (file exists but content diverged from canonical) —
  `bootstrap` never overwrites an existing `tests.yaml` (write-if-absent
  scaffold). Regenerating it requires deleting the file first, which would
  also discard any legitimate app-specific customization outside the
  recognized param set (app-name, app-image-name, enable-e2e,
  services-script). `classification = "judgment"` — a human must decide
  whether the drift is stale structure (safe to delete + regenerate) or an
  intentional customization worth preserving as-is.
- **`renovate.json` drift** (file exists but content diverged) — fixing it
  requires choosing the intended enforcement mode (`--enforce true|false`),
  a repo-policy decision the model must not make unilaterally.
  `classification = "judgment"`.

---

**C003 GitignoreMissingEntry** — two distinct cases with different
remediability:

- **`.gitignore` is absent** — `classification = "mechanical"`. Already
  covered by the C002 `bootstrap` invocation above (step 3): it scaffolds a
  standard `.gitignore` whenever none exists. No separate action needed; if
  a C002 pass already ran this remediation round, this finding is already
  cleared by the time `remediate-finding` sees it.
- **`.gitignore` exists but is missing a standard entry** — **not** resolved
  by `bootstrap` (it only ever writes `.gitignore` when the file is
  entirely absent; it never edits an existing one). `classification =
  "judgment"` — route to residue. A human should confirm the missing entry
  is actually wanted before it's appended (e.g. a repo that deliberately
  tracks `.env` for a documented reason).

---

**C001 UnpinnedActionReference** — a `uses:` reference is pinned to a mutable
tag or branch instead of a full 40-hex commit SHA. `classification =
"mechanical"` (resolving a ref to its current commit is deterministic, not an
interpretive call) but `external_influence = true` (the SHA comes from a live
GitHub lookup, not the source file itself) — see the Write-scope constraint
in `remediate-finding.prose.md` for why this is the one narrow exception to
the "no other rule may touch `.github/`" rule, and why it is nonetheless
always escalated to residue.

*Procedure*, per finding:

1. Read `finding.message` (or the actual line at `finding.file`:`finding.line`)
   to get the exact `owner/repo[/path]@<ref>` string. Do not use the message's
   paraphrase for the replacement — re-derive `owner/repo` and `ref` from the
   literal `uses:` line so whitespace/quoting is preserved exactly.
2. Resolve `ref` to its full 40-hex commit SHA:
   - Preferred (handles tags, branches, and short SHAs uniformly): `gh api
     repos/<owner>/<repo>/commits/<ref> --jq .sha`.
   - Fallback when `gh` is unavailable/unauthenticated (tags and branches
     only — cannot expand an already-short SHA): `git ls-remote
     https://github.com/<owner>/<repo> <ref>`. If the ref is an annotated tag,
     `git ls-remote --tags` returns both `refs/tags/<ref>` and the peeled
     `refs/tags/<ref>^{}` — always prefer the peeled (`^{}`) SHA, which is the
     commit the tag points to, not the tag object itself.
   - If neither resolves (no network egress, ref not found, ambiguous match),
     `not_remediable = true` for this finding — do not guess a SHA.
3. Rewrite the `uses:` line's ref suffix only:
   `uses: <owner>/<repo>[/path]@<ref>` → `uses:
   <owner>/<repo>[/path]@<full-sha> # <original-ref>`. The trailing `#
   <original-ref>` comment is required, not cosmetic — it's what keeps the
   pin human-auditable and matches the exact comment convention C002's own
   drift check already tolerates (`_ACTION_PIN_RE` in `bootstrap_drift.py`
   strips `@<sha> # ...` before comparing, so this never causes new C002
   drift). Change nothing else on the line or in the file.
4. `outcome = "fix"`. `orthogonal_gate = "none"` (set on the C001 rule
   definition) — a `.github/` ref-pin change cannot affect Python behaviour,
   so the test suite is skipped; `recheck-narrowest` is the only gate.
5. Regardless of `classification` (`"mechanical"`) and regardless of
   `recheck.clear`, `external_influence = true` sends this finding down
   `detect-fix-recheck`'s existing residue branch. This is intentional, not a
   fallback: the resolved SHA reflects whatever the tag/branch currently
   points to, and nothing about a successful re-check can validate whether
   that commit is trustworthy — a human must eyeball the diff (owner/repo,
   resolved SHA, and the preserved ref comment) before it merges.
