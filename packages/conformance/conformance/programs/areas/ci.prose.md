---
kind: responsibility
name: ci-area
description: >
  Maintains the current C-series violation-set.  C002 (and C003's
  absent-`.gitignore` case) are mechanically remediated by invoking
  `atlan-application-sdk-conformance bootstrap` — a deterministic re-sync, not
  a model-authored edit.  C001 is mechanically pinned — the mutable ref is
  resolved to a commit SHA and the `uses:` line rewritten, a deterministic
  step — but always routed to residue for mandatory human sign-off afterward,
  since a live-resolved SHA cannot itself be judged trustworthy by any
  recheck; this is assisted, not autonomous, remediation.  C003's
  missing-entry case and drifted `tests.yaml`/`renovate.json` still have no
  authored prescription and route to residue for manual triage.
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
> after every mutable ref is repinned to a resolved SHA — though on a passing
> recheck, every C001 fix is additionally escalated to residue for mandatory
> human sign-off (`external_influence`); a C001 fix whose recheck fails is
> reverted and residues via the ordinary "recheck failed" path instead, like
> any other rule.  Any C-series findings that remain
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
`atlan-application-sdk-conformance bootstrap` would write.
`classification = "mechanical"` — the fix is a deterministic re-sync, not a
model-authored edit: `bootstrap` overwrites managed files with the exact
canonical rendered template the C002 checker itself renders for comparison,
so there is nothing for the model to invent or judge.

*Procedure* — run once per remediation pass; one invocation re-syncs every
managed file, so it clears every other pending C002 (and C003 absent-file)
finding in the same pass. Also run this same procedure directly when handling
a standalone C003 absent-`.gitignore` finding — see that section below for why
it isn't safe to assume a C002 finding already triggered it:

1. Run, with no flags:
   ```
   atlan-application-sdk-conformance bootstrap
   ```
   `bootstrap` auto-detects every per-repo customization itself — `--app-name`
   and `--package-name` from `atlan.yaml`/an existing
   `docstring-coverage.yaml` (else the repo directory name/`"app"`),
   `--unit-tests-workflow` from an existing `build-and-publish.yaml` (else
   `"tests.yaml"`), `--services-script` from an existing
   `.github/test/setup-services.sh`, and `--enforce` from an existing
   `conformance.yaml`'s `exit-zero` mode (else hard-gate). No extraction step
   is needed here; re-running with no flags never resets a customized value
   to a default.
2. This single command resolves, in one pass: every absent/drifted managed
   workflow shim, every absent/drifted vendored action file, an absent
   `.gitignore` (C003's absent-file case — see below), an absent
   `renovate.json`, and an absent `tests.yaml`.
3. `outcome = "fix"`. `touched_files` = every path `bootstrap` printed with a
   `scaffolded:`, `installed:`, `updated:`, or `backed up:` prefix in this
   invocation's stdout — see the write-scope note in
   `remediate-finding.prose.md` for why this is deterministic (read off the
   CLI's own output, not model-judged) and how it drives
   `detect-fix-recheck`'s revert scope if this fix is later rejected by a
   gate. This no-flags procedure never triggers a `backed up:` line itself
   (that only happens when `--enforce` is passed explicitly), but capture it
   if present rather than assuming the three-prefix list from other contexts
   is exhaustive here too. `orthogonal_gate = "skip"` on both C002 and C003
   (set on the rule definitions) — the Python test suite is skipped entirely,
   not just for this fix: a `.github/`/scaffold-only change cannot affect
   Python behaviour. `recheck-narrowest` (re-running `suite.runner --series
   C`) and the parseability check `orthogonal-gate.prose.md`'s `"skip"`
   branch runs over every touched YAML/JSON file are the only verification
   that applies — C002/C003 fixes are not unconditionally escalated to
   residue the way C001's are, so that parseability check is what catches a
   syntax-breaking rewrite before it auto-accepts.
4. Any C002 finding still present after this pass is genuinely not
   bootstrap-fixable and falls into the residue cases below.
5. `bootstrap`'s write footprint is wider than `.github/`/`.gitignore` —
   see the write-scope note in `remediate-finding.prose.md` for the full
   accounting, all captured by the same `touched_files` extraction from
   step 3: `.claude/skills/remediate/SKILL.md` (and the SDK-repo exception to
   overwriting it, enforced in `conformance/bootstrap/command.py`), and
   `contract_schema.lock.json`'s
   new-baseline residue note when it is created rather than already present.

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

- **`.gitignore` is absent** — `classification = "mechanical"`. Run the C002
  `bootstrap` procedure above directly for this finding too: it scaffolds a
  standard `.gitignore` whenever none exists. Do **not** assume a C002
  finding already triggered this in the same pass — `detect-fix-recheck`
  batches findings per file, so a repo with a missing `.gitignore` but no
  other managed-file drift (zero C002 findings that round) would otherwise
  have nothing invoke `bootstrap`, and `recheck-narrowest` would then find
  `.gitignore` still absent and misroute this to residue as "recheck
  failed" even though it's fully mechanical. `bootstrap` is idempotent, so
  invoking it again when a C002 pass already ran earlier in the same round
  is harmless.
- **`.gitignore` exists but is missing a standard entry** — **not** resolved
  by `bootstrap` (it only ever writes `.gitignore` when the file is
  entirely absent; it never edits an existing one).
  `classification = "judgment"` — route to residue. A human should confirm
  the missing entry is actually wanted before it's appended (e.g. a repo
  that deliberately tracks `.env` for a documented reason).

---

**C001 UnpinnedActionReference** — a `uses:` reference is pinned to a mutable
tag or branch instead of a full 40-hex commit SHA.
`classification = "mechanical"` (resolving a ref to its current commit is
deterministic, not an interpretive call) but `external_influence = true`
(the SHA comes from a live GitHub lookup, not the source file itself) — see
the Write-scope constraint
in `remediate-finding.prose.md` for why this is the one narrow exception to
the "no other rule may touch `.github/`" rule, and why it is nonetheless
always escalated to residue.

*Procedure*, per finding:

1. Read `finding.message` (or the actual line at `finding.file`:`finding.line`)
   to get the exact `owner/repo[/path]@<ref>` string. Do not use the message's
   paraphrase for the replacement — re-derive `owner/repo` and `ref` from the
   literal `uses:` line so whitespace/quoting is preserved exactly.
2. **Validate before running anything.** `owner`, `repo`, and `ref` come from
   a file under `.github/`, which is exactly the content a finding's source
   branch may not be trustworthy about (e.g. remediation running against an
   unreviewed branch) — never interpolate them into a shell command without
   validating first. Reject the finding (`not_remediable = true`; do not
   proceed to step 3) unless `owner` and every `repo`/path segment match
   `^[A-Za-z0-9._-]+$` and `ref` matches `^[A-Za-z0-9._/-]+$`. When invoking
   `gh api` / `git ls-remote` below, pass `owner`, `repo`, and `ref` as
   separate literal command arguments — never string-concatenate them into a
   shell line that could re-interpret metacharacters.
3. Resolve `ref` to its full 40-hex commit SHA:
   - Preferred (handles tags, branches, and short SHAs uniformly):
     `gh api repos/<owner>/<repo>/commits/<ref> --jq .sha`.
   - Fallback when `gh` is unavailable/unauthenticated (tags and branches
     only — cannot expand an already-short SHA):
     `git ls-remote https://github.com/<owner>/<repo> <ref>`. If the ref is
     an annotated tag, `git ls-remote --tags` returns both
     `refs/tags/<ref>` and the peeled `refs/tags/<ref>^{}` — always prefer
     the peeled (`^{}`) SHA, which is the commit the tag points to, not the
     tag object itself.
   - If neither resolves (no network egress, ref not found, ambiguous match),
     `not_remediable = true` for this finding — do not guess a SHA.
4. Rewrite the `uses:` line's ref suffix only, from
   `uses: <owner>/<repo>[/path]@<ref>` to
   `uses: <owner>/<repo>[/path]@<full-sha> # <original-ref>`.
   The trailing `# <original-ref>` comment is required, not cosmetic — it's
   what keeps the pin human-auditable and matches the exact comment
   convention C002's own drift check already tolerates (`_ACTION_PIN_RE` in
   `bootstrap_drift.py` strips `@<sha> # ...` before comparing, so this
   never causes new C002 drift). Change nothing else on the line or in the
   file.
5. `outcome = "fix"`. `orthogonal_gate = "skip"` (set on the C001 rule
   definition) — a `.github/` ref-pin change cannot affect Python behaviour,
   so the test suite is skipped; `recheck-narrowest` plus the `"skip"`
   branch's YAML parseability check (see `orthogonal-gate.prose.md`) are the
   only gates. Belt-and-suspenders here since step 6 always routes this
   finding to residue for human sign-off regardless of gate outcome, but the
   parseability check still means a syntax-breaking rewrite is caught and
   reverted automatically rather than reaching residue at all.
6. On a passing recheck, regardless of `classification` (`"mechanical"`),
   `external_influence = true` sends this finding down
   `detect-fix-recheck`'s existing residue branch. This is intentional, not a
   fallback: the resolved SHA reflects whatever the tag/branch currently
   points to, and nothing about a successful re-check can validate whether
   that commit is trustworthy — a human must eyeball the diff (owner/repo,
   resolved SHA, and the preserved ref comment) before it merges. A recheck
   that fails takes the ordinary `detect-fix-recheck` path instead — reverted
   and residued as "recheck failed" — and never reaches this branch.
