# 1b-toolkit. Private Toolkit Consumer Setup

**mothership sandbox only.** On `LANE: sdk-loop` this file must not be
followed: the consumer clones below need credentials for private atlanhq
repos that the loop lane's scoped App token does not carry, and every clone
fails with `could not read Username`. The router's 1b-toolkit pointer gives
that lane its fallback (a Rover note + static surface review). Measured on a
live run: five clone failures, then an idle-watchdog kill with no verdict.

### 1b-toolkit. Private Toolkit Consumer Setup (if review_scope=contract-toolkit or mixed-sdk-toolkit)

Read:

- `contract-toolkit/AGENTS.md`
- `.mothership/pr-review/agents/toolkit-review.md`
- `.mothership/pr-review/references/toolkit-consumer-registry.md`

Classify the affected toolkit surfaces from `/tmp/DIFF.patch`. Then clone or
reuse every mandatory consumer target from the registry. This validation setup
is mandatory for affected surfaces; do not approve if a required check cannot
run.

Reset and create the private validation ledger (these files are written only
here, so the reset lives here — not in Phase 0). It is the source of truth
for toolkit compatibility status and verdict gating:

```bash
rm -f /tmp/TOOLKIT_ROVER_NOTE.md
: > /tmp/TOOLKIT_PR_ARTIFACTS.txt
: > /tmp/TOOLKIT_CHANGED_FILES.txt
: > /tmp/TOOLKIT_CONSUMERS.md
: > /tmp/TOOLKIT_VALIDATION.md
printf '## Toolkit Validation Ledger\n\n' >> /tmp/TOOLKIT_VALIDATION.md
```

**Do not re-run what CI already ran.** The `Contract Toolkit /` CI legs on
this HEAD run the same commands the reviewer used to re-run wholesale
(`PKL tests and invariants`, `Verify generated output`, `Generated Python
lint and SDK imports`). Read them instead:

```bash
# `bucket`, NOT `conclusion`. `gh pr checks --json` has no `conclusion`
# field: it prints "Unknown JSON field" to stderr, EXITS 0, and writes
# nothing to stdout — so this file would be empty and every leg would read
# as missing. Buckets are pass / fail / skipping / pending.
gh pr checks "$PR_NUMBER" --repo "$REPO" --json name,bucket \
  --jq '.[] | select(.name | startswith("Contract Toolkit")) | .name + " " + .bucket' \
  > /tmp/TOOLKIT_CI_LEGS.txt
```

- Every leg `pass` → record the legs as the local-check evidence in the
  ledger. Run a local command ONLY as the substrate for probing CI cannot
  express — guard ablations, scratch collision contracts, comparing
  PR-generated artifacts against a consumer's expectations.
- Any leg missing / pending / failed → run that leg's command locally and
  treat a failure caused by PR code or stale generated output as a finding:

```bash
contract-toolkit/scripts/regenerate-all.sh     # ↔ Verify generated output
contract-toolkit/scripts/check-invariants.sh   # ↔ PKL tests and invariants
(cd contract-toolkit && pkl test tests/*.pkl)  # ↔ PKL tests and invariants
uv run --extra workflows python contract-toolkit/scripts/test-sdk-import.py  # ↔ Generated Python lint and SDK imports
git diff --check
```

If a required command cannot run due to Rover environment/tooling failure,
create `/tmp/TOOLKIT_ROVER_NOTE.md` with the sanitized note below.

Record the evidence privately:

```bash
printf -- '- Generated SDK input contract: validated (CI toolkit legs green on this HEAD; PR-bound probing ran locally)\n' >> /tmp/TOOLKIT_VALIDATION.md
```

Capture PR-generated artifacts as the input to downstream checks. Do not use a
consumer repository's released toolkit dependency as proof for this PR:

```bash
find contract-toolkit/examples -path '*/generated/*' -type f | sort > /tmp/TOOLKIT_PR_ARTIFACTS.txt
git diff --name-only -- contract-toolkit/examples contract-toolkit/src > /tmp/TOOLKIT_CHANGED_FILES.txt
```

**Carry-forward fast path (re-reviews).** Hash the PR-generated artifacts
and compare against the hash the prior review stamped (§3e stamps
`<!-- TOOLKIT_ARTIFACT_HASH: ... -->` on toolkit-scope summaries):

```bash
ARTIFACT_HASH=$(cat /tmp/TOOLKIT_PR_ARTIFACTS.txt | xargs shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
PRIOR_HASH=$(grep -oE '<!-- TOOLKIT_ARTIFACT_HASH: [0-9a-f]{64} -->' /tmp/PRIOR_REVIEW.md \
  | grep -oE '[0-9a-f]{64}' || true)
```

If `PRIOR_HASH` is non-empty and equals `ARTIFACT_HASH`, the generated
artifacts are byte-identical to a commit whose downstream compatibility was
already validated: mark every artifact-derived capability
`validated (carried forward — artifacts byte-identical to previously
validated commit)` in the ledger and **skip the consumer clone loop
entirely**. Carry-forward covers the *consumer-side* checks only — new
toolkit-source behavior the committed examples don't exercise (a new
invariant, a changed codegen skip-list) still needs PR-bound local probing
(scratch contracts, ablations), which requires no clones. Hash mismatch or
no prior hash → full validation below.

Use `/tmp/toolkit-review-consumers` for scratch clones:

```bash
mkdir -p /tmp/toolkit-review-consumers

# Core consumers. Use existing /workspace checkout if present; otherwise clone.
# Record branch and SHA privately in /tmp/TOOLKIT_CONSUMERS.md.
for spec in \
  "atlan-frontend beta" \
  "blaze main" \
  "heracles beta" \
  "atlan-automation-engine-app main"
do
  repo="${spec% *}"
  branch="${spec#* }"
  if [ -d "/workspace/${repo}/.git" ]; then
    target="/workspace/${repo}"
  else
    target="/tmp/toolkit-review-consumers/${repo}"
    if [ ! -d "${target}/.git" ] && ! git clone "https://github.com/atlanhq/${repo}.git" "${target}"; then
      printf '%s\n' "Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge." > /tmp/TOOLKIT_ROVER_NOTE.md
      continue
    fi
  fi
  if ! git -C "${target}" fetch origin "${branch}"; then
    printf '%s\n' "Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge." > /tmp/TOOLKIT_ROVER_NOTE.md
    continue
  fi
  printf '%s %s %s\n' "${repo}" "origin/${branch}" "$(git -C "${target}" rev-parse "origin/${branch}")" >> /tmp/TOOLKIT_CONSUMERS.md
done
```

Cloning/fetching only establishes the validation target. It is not validation.
For each affected capability, run the corresponding minimum actionable check in
the registry using PR-generated artifacts or a scratch contract rewritten to
amend/import `/workspace/application-sdk/contract-toolkit/src/*.pkl`. If no
PR-bound command or inspection is possible, mark that capability `needs rerun`
and do not approve.

Each mandatory capability must append exactly one private status line to
`/tmp/TOOLKIT_VALIDATION.md`:

```text
- UI rendering compatibility: validated (<private evidence recorded>)
- Manifest substitution compatibility: validated (<private evidence recorded>)
- Workflow execution contract: validated (<private evidence recorded>)
- Generated SDK input contract: validated (<private evidence recorded>)
- Representative app pattern: not applicable (<why>)
```

Allowed statuses are `validated`, `not applicable`, and `needs rerun`. Any
`needs rerun` status forces `NEEDS_HUMAN`.
The public review must mirror these as a `### Cross-Repo Validation` section
using only capability aliases and status values. Do not include private
consumer repository names, package names, branch names, SHAs, local paths, or
system-app implementation details in the public section.

For representative app patterns, inspect PR title, body, and diff for trigger
terms from the registry. Clone/fetch only the matching pattern repos. Optional
field additions do not require adoption in the representative app unless the PR
claims compatibility or changes required generated/runtime behavior.

Use these pattern specs after a trigger match:

```bash
# Pattern specs: "<pattern> <repo> <branch>"
# query-intelligence atlan-query-intelligence-app main
# publish atlan-publish-app main
# popularity atlan-popularity-app main
# lineage atlan-lineage-app main
```

For each matched pattern, reuse `/workspace/<repo>` if present, otherwise clone
to `/tmp/toolkit-review-consumers/<repo>`, fetch the listed branch, and append
the private SHA to `/tmp/TOOLKIT_CONSUMERS.md`.

When validating a representative app contract, work only in a scratch copy. If
the contract imports `@app-contract-toolkit/...`, rewrite that scratch copy to
import the PR checkout source under
`/workspace/application-sdk/contract-toolkit/src/` before running `pkl eval`.

Scratch rewrite pattern:

```bash
scratch="/tmp/toolkit-review-consumers/scratch/<pattern>"
mkdir -p "$scratch"
cp -R "<consumer-contract-dir>"/. "$scratch"/
rg -l '@app-contract-toolkit/' "$scratch" \
  | xargs perl -0pi -e 's#@app-contract-toolkit/#/workspace/application-sdk/contract-toolkit/src/#g'
pkl eval -m "$scratch/generated" "$scratch/app.pkl"
```

If the representative app does not have a contract yet, do not fail adoption.
Validate the generic PR-generated artifact shape and record the representative
pattern as `not applicable` with the reason.

All consumer repo names, local paths, and SHAs are private evidence. Public PR
comments may only use capability aliases:

- `UI rendering compatibility`
- `Manifest substitution compatibility`
- `Workflow execution contract`
- `Generated SDK input contract`
- `Representative app pattern`

If clone/fetch/auth/network fails for a mandatory target, create
`/tmp/TOOLKIT_ROVER_NOTE.md` with exactly this public note and continue to
Phase 2 so the review can request a rerun or human review:

```text
Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge.
```
