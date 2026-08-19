# Conformance Remediation — Orchestration Playbook

Follow ALL stages (0–8). NEVER stop early. NEVER defer work to "later".
Print `[Stage N/8 complete]` after each stage.

**Hard stop: 75 minutes** (single-unit runs) or **`TIME_BUDGET_MINUTES` from
the dispatch prompt** (batch runs). If you approach it, skip to Stage 8 and
report what actually happened with an honest `N of M` — and in batch mode,
report every unit you never started as `not-reached`.

## Run context

The dispatch prompt passes these verbatim — read them, do NOT re-derive:

```
REPO           e.g. atlanhq/atlan-netsuite-app
RULE_ID        e.g. L004          — exactly one rule
SERIES         e.g. L             — RULE_ID[0]; the narrowest series to scan
TIER           block | warn
DELIVERY       push_to_pr_branch | one_pr_per_rule
BASE_REF       the PR head ref (push_to_pr_branch) or main (one_pr_per_rule)
HEAD_SHA       expected HEAD at clone time (push_to_pr_branch only)
PR_NUMBER      the PR being fixed (push_to_pr_branch only)
SUITE_VERSION  e.g. 0.20.1        — pinned; never "latest"
APPLY_UNVERIFIABLE  true | false
RUN_ID         the orchestrator's run id, for the report
```

---

## Batch mode — one rover, many units

If `/workspace/.mothership/session/BATCH.json` exists, this run is a **batch**:
one sandbox working a pre-triaged manifest of units across up to ~10 app repos.
The orchestrator has already done the deterministic analysis — every unit in
the manifest has unsuppressed findings, no fresh open lane PR, and no
human-declined PR — so the manifest IS the worklist. Do not re-plan it.

```
BATCH.json:
  batch_id              e.g. crb-1a2b3c4d5e6f
  suite_version         pinned; never "latest"
  apply_unverifiable    true | false
  time_budget_minutes   your hard stop for STARTING new units
  units[]               ordered: { repo, rule_id, tier, autofixable }
```

Rules of the batch:

1. **Work app by app, in manifest order.** Finish every unit of one repo before
   moving to the next. Only the FIRST repo is pre-cloned under `/workspace/`;
   clone each subsequent repo yourself when you reach it:
   `gh repo clone <owner/repo> /workspace/<name> -- --depth 50`
2. **Per unit, run Stages 0–7 exactly as written below**, with `REPO`,
   `RULE_ID`, `TIER` taken from the manifest entry, `SERIES = RULE_ID[0]`,
   `DELIVERY = one_pr_per_rule`, `BASE_REF = main`, and `SUITE_VERSION` /
   `APPLY_UNVERIFIABLE` from BATCH.json. Everything holds per unit: reconcile
   first, branch before any edit, ONE PR per rule per repo, never mix rules in
   one branch.
3. **Prior rulings are keyed** — entries in `PRIOR_DECISIONS.json` carry
   `repo` + `rule_id`; apply only the ones matching the unit in hand.
4. **The time budget bounds STARTS, not finishes.** Check elapsed time between
   units. Past `time_budget_minutes`, do not start another unit: finish (or
   cleanly abandon — revert, no push) the one in flight, then report every
   remaining unit as `not-reached`. An honest partial batch is a success; a
   rushed fix is not.
5. **One stuck unit never strands the batch.** A unit that ends in
   `rule-review` or `error` is recorded as such and you MOVE ON. Never retry a
   unit inside the batch — the orchestrator owns retries.
6. **Stage 8 is replaced by the batch report** (see *Stage 8 — batch contract*
   below): one JSON line per manifest unit in `=== BATCH RESULTS ===`, every
   unit exactly once, then `RESULT: batch:<completed>/<total>`.

Per-unit result kinds in a batch (superset of the single-unit kinds):

| result | meaning |
|---|---|
| `pushed` | new lane PR opened (or new commits on a fresh branch) |
| `refreshed` | an existing stale open lane PR was updated in place — same branch, plain push |
| `exists` | an adequate open PR already covers the rule; zero work |
| `no-op` | pinned detect found nothing unsuppressed — findings were stale |
| `declined-respected` | newest lane PR was closed unmerged by a human — veto honoured, app untouched |
| `rule-review` | the rule is wrong/gapped: SDK fix PR raised (see Stage 6), app untouched |
| `error` | the unit failed; detail says how |
| `not-reached` | time budget expired before this unit started |

---

## Stage 0: Preflight

```bash
cd /workspace/$(basename "$REPO")
echo "$GITHUB_TOKEN" | gh auth login --with-token
git rev-parse HEAD
```

1. Read `/workspace/.mothership/session/PRIOR_DECISIONS.json`. Load every ruling
   keyed by fingerprint. These are binding for this run.
2. **`push_to_pr_branch` only:** assert `git rev-parse HEAD` equals `HEAD_SHA`. If
   it does not, the branch moved between scheduling and now — stop with
   `error: head moved (expected $HEAD_SHA, got <actual>)`. Fixing a commit the
   orchestrator never inspected is how you push a change nobody asked for.
3. Warm deps in the background — the orthogonal gate needs them:
   `uv sync --all-extras 2>/dev/null &`

Print: `[Stage 0/8 complete] repo=$REPO rule=$RULE_ID delivery=$DELIVERY`

---

## Stage 1: Reconcile — never duplicate, refresh what has gone stale

Before any work, establish what already exists for this unit and act on it.
A second PR for the same rule is noise; an open PR that no longer clears the
rule is worse than noise — it reads as "handled" while the gap regrows.

**`one_pr_per_rule`:**

```bash
BRANCH="conformance/$(echo "$RULE_ID" | tr '[:upper:]' '[:lower:]')"
gh pr list --repo "$REPO" --head "$BRANCH" --state all \
  --json url,state,mergeable,updatedAt,mergedAt
```

Route on what you find:

| Prior PR state | Action |
|---|---|
| **None** (no branch, no PR) | proceed to Stage 2 — fresh unit |
| **MERGED** | the fix landed. Run Stage 3's detect anyway: if the rule is clean → `RESULT: no-op: fixed by <url>`; if NEW findings appeared since the merge → proceed as a fresh unit. The old branch is gone with the merge; if a stale branch somehow remains, suffix yours `-r<YYYYMMDD>` — never force-push over history |
| **OPEN, still adequate** | `RESULT: exists:<url>` — nothing to do. "Adequate" is checked, not assumed: see below |
| **OPEN, not doing justice** | **refresh it in place** — see the refresh procedure |
| **CLOSED without merge** | a human looked at this and said no. **Respect that**: `RESULT: exists:<url> (closed unmerged — human decision; not recreating)`. Only a human (`/remediate`) reopens this conversation |

**"Adequate" is a measurement.** Check both, cheaply, before deciding:

1. `mergeable != CONFLICTING` — a conflicted PR cannot land, so it is not doing
   its job whatever its diff says.
2. Coverage: fetch the PR branch, run the pinned detect **on the PR branch**
   filtered to `RULE_ID`. Zero findings there AND the PR branch contains every
   finding location currently failing on `main` → adequate. If `main` has grown
   NEW findings for this rule since the PR was cut, the PR under-covers → refresh.

**The refresh procedure** (open PR, conflicted or under-covering):

```bash
git fetch origin "$BRANCH" && git switch "$BRANCH"
git merge origin/main          # resolve conflicts favouring main's structure
# then run Stages 3-5 as normal ON THIS BRANCH for the remaining findings
git push origin "$BRANCH"      # plain push — NEVER --force; history is shared
```

Update the PR body: keep the original table, append a `### Refreshed <date>`
section stating what changed and the new N of M. Emit
`RESULT: pushed:<sha> (refreshed <url>)`. The PR keeps its number, its
reviewers, and its discussion — a refresh is a new commit on a shared branch,
never a rewrite of one.

**`push_to_pr_branch`:** check whether a prior run already landed this rule on
this PR — look for a commit on the branch whose subject ends `($RULE_ID)`. If
found and the rule now detects clean, that is `no-op: already fixed on this branch`.

Print: `[Stage 1/8 complete] <fresh|exists|refresh|declined|merged-clean>`

---

## Stage 2: Branch — BEFORE the first edit

Ordered deliberately. An edit made before a branch exists is an edit on
`BASE_REF`, and any mid-run checkpointing would push it there.

**`one_pr_per_rule`:**
```bash
git switch -c "$BRANCH" "origin/$BASE_REF"
```

**`push_to_pr_branch`:** you are already on the PR branch from the clone. Do not
create a branch. Record the starting SHA so Stage 6 can diff against it:
```bash
START_SHA=$(git rev-parse HEAD)
```

Print: `[Stage 2/8 complete] branch=<name> start=<sha>`

---

## Stage 3: Detect — ground truth, pinned

The published SARIF that scheduled this unit is a **hint**. This is the truth.

```bash
mkdir -p /tmp/remediation
uvx "atlan-application-sdk-conformance==${SUITE_VERSION}" detect \
  --repo . --series "$SERIES" --output /tmp/remediation/before.sarif
jq '.runs[0].properties["atlan/summary"]' /tmp/remediation/before.sarif
```

**D-series only** — D003 resolves import names from installed package metadata,
so it needs the app's own environment; a bare `uvx` run degrades it:
```bash
uv sync --all-extras
uv run --with "atlan-application-sdk-conformance==${SUITE_VERSION}" \
  atlan-application-sdk-conformance detect --repo . --series D \
  --output /tmp/remediation/before.sarif
```

Filter the results to `RULE_ID` and record `M` = the count of unsuppressed
findings whose disposition matches `TIER`. If `M == 0`, emit
`RESULT: no-op: rule $RULE_ID detects clean at $SUITE_VERSION` and go to Stage 8 —
a real detection found nothing, which is a legitimate and useful answer.

Print: `[Stage 3/8 complete] $M finding(s) for $RULE_ID`

---

## Stage 4: Fan out to sub-agents (the parallel half)

Group the `M` findings by file. For each file, dispatch **one `Task` sub-agent**.
Sub-agents run on the cheap model; you do not. Two jobs per file, and they can be
one sub-agent each or one combined — but they must be sub-agents, not you:

**(a) Draft the edits.** Give the sub-agent: the file's findings with line numbers,
the rule's area prescription from `$PROGRAMS/areas/<area>.prose.md`, the rule's
`full_description` and `rationale`, and any prior decisions for those fingerprints.
Ask for the exact edits — old text and new text — and nothing else.

**(b) Try to refute.** Ask a sub-agent to argue that each finding is a **false
positive**, citing code. Refute, not confirm: a sub-agent asked "is this real?"
agrees with the detector almost every time.

Constraints, both absolute:

- **Sub-agents never write files.** They return proposed edits as text. You are
  the single writer. Parallel writers race and corrupt.
- **A refutation must cite code** — a line, a symbol, a construct that makes the
  detector wrong. "This looks intentional" is not a refutation.

Print: `[Stage 4/8 complete] <N> file(s) fanned out, <R> refutation(s) returned`

---

## Stage 5: The gated loop (the serial half — you)

For each finding, in file batches:

1. **Prior ruling?** Honour it. Do not re-decide.
2. **Refuted?** Skip the fix. Record a decision with the citation. This finding
   contributes to `rule-review`, never to a silent suppression.
3. **`APPLY_UNVERIFIABLE` and this is a P- or S-series finding?** The chosen value
   must carry a **cited source** (a contract schema field, a documented upstream
   limit, the secret-store path or env-var NAME it now reads). No citation ⇒ do
   not apply; record `no-cited-evidence`. Check this *before* writing, so a
   guessed value never reaches the tree.
4. **Apply** the edit.
5. **Re-check, narrowest:** re-run detect for `SERIES`, filtered to this file and
   this fingerprint. Not gone ⇒ **revert** and record `recheck-failed`.
6. **Orthogonal gate**, dispatched on the rule's `orthogonal_gate`:
   - `tests` / unset → `uv run poe test`
   - `docker-build` → `docker info` first; unavailable ⇒ **revert** and record
     `cannot-verify` (never treat an unrunnable gate as a pass). Otherwise
     `docker build` the touched Dockerfile.
   - `pkl-eval` → the pkl-eval gate
   - `skip` → parse-check every touched non-Python file
   Fail ⇒ **revert** and record `orthogonal-gate-failed`.
7. Loop, re-detect, compare fingerprint sets. **Identical set across rounds =
   oscillation**: stop immediately and record it. Cap: **5 attempts**.

**Never edit `tests/`, `.github/` or `conformance/` to make a gate pass.** If the
only way to clear a finding is to change what judges you, the answer is
`rule-review`.

### Adjudication policy — when a prescription leaves the choice open

| Situation | Ruling |
|---|---|
| Two valid fixes, one narrower | take the narrower; record why the broader was rejected |
| The fix would need an edit outside this rule's concern | do **not** widen — `rule-review` |
| Suppression is the only clearing move | suppress **with** an 8–40 word justification quoting the evidence; flag for human |
| Gate is blind (P/S) | apply only with a cited source; flag for human |
| `forces_external_influence` (C001) | apply, record the resolved SHA as evidence; flag for human |
| Oscillation, or the attempt cap | stop; `rule-review` with the fingerprint set. Never keep thrashing |
| Genuinely ambiguous, no evidence either way | **do nothing**; `rule-review`. Abstaining is a valid ruling |

Print: `[Stage 5/8 complete] fixed=<n> reverted=<n> refuted=<n> decisions=<n>`

---

## Stage 6: Rule review — fix the rule, don't file a ticket

Reached when Stage 5 produced any `rule-review` finding: a refutation with a code
citation, a `recheck-failed`, an `orthogonal-gate-failed`, an oscillation, or a
`not_remediable`. Each of those is evidence the **rule** is wrong, not the app.

Do not leave that as a note for someone. Open the fix.

**Before anything else — decide whether you can.** A conformance rule runs against
every repo in the fleet, so narrowing one wrongly stops it catching real defects in
53 other apps and nobody notices, because the symptom is silence. If you cannot
articulate the exact shape that should not fire, **abstain**: emit
`RESULT: rule-review: no fix drafted — <what you could not establish>` with the
evidence, and stop. An un-opened PR is recoverable; a wrongly-narrowed detector is
a fleet-wide blind spot.

If you can:

```bash
cd /workspace
gh repo clone atlanhq/application-sdk sdk -- --depth=50
cd sdk
RULE_BRANCH="conformance/rule-fix/$(echo "$RULE_ID" | tr '[:upper:]' '[:lower:]')"
git ls-remote --exit-code --heads origin "$RULE_BRANCH" && echo "already open"
gh pr list --repo atlanhq/application-sdk --head "$RULE_BRANCH" --state all --json url
```

Already open ⇒ comment your new evidence on that PR and report
`rule-review:<existing-url>`. One rule-fix PR per rule, same as one remediation PR
per rule.

Otherwise:

1. `git switch -c "$RULE_BRANCH" origin/main`
2. **Narrow the detector** in `packages/conformance/conformance/suite/checks/` (or
   the rule's `RuleDefinition` where the fix is metadata). Narrow to the shape you
   have evidence for and nothing wider.
3. **Add the regression test — mandatory.** In
   `packages/conformance/tests/test_<series>.py`, add a `fires`/`silent` pair in
   the file's existing style. The `silent` case is the exact code shape you found
   in `$REPO`. This is the deliverable: the fix without the test is a change nobody
   can defend, and the file's own header says a buggy check "false-positives across
   hundreds of apps and triggers spurious remediations".
4. Verify, all three:
   ```bash
   cd packages/conformance
   uv sync --all-extras --all-groups
   uv run pytest -q
   uv run atlan-application-sdk-conformance gen-rule-docs --check
   ```
   Any of them red ⇒ do not push. Report `rule-review: fix drafted but <gate> failed`.
5. Push and open the PR:
   ```bash
   git push origin "$RULE_BRANCH"
   gh label create conformance-rule-fix --repo atlanhq/application-sdk \
     --color 5319E7 --description "Narrows a conformance rule (remediation rule-review path)" || true
   gh pr create --repo atlanhq/application-sdk --base main \
     --title "fix(conformance): $RULE_ID no longer fires on <the shape>" \
     --label conformance-rule-fix \
     --body-file /tmp/remediation/rule_fix_body.md
   ```
   The body must carry: the rule ID, the **app repo, file and fingerprint** that
   motivated it, the refutation's code citation, what shape now stops firing, what
   still does, and a link to `RUN_ID`.
6. **Hand it to the resolver.** Comment `@sdk-resolve` on the new PR. That drives
   it through review→fix→push until it is merge-ready, then stops — a human still
   merges. So this stage ends with a PR that keeps improving itself rather than one
   that waits for someone to notice it.
   ```bash
   gh pr comment <number> --repo atlanhq/application-sdk --body "@sdk-resolve"
   ```

**The upstream-gap case.** Sometimes the rule is *right* and the app is
*wrong*, but the correct fix is blocked on something neither owns — e.g. a
`creator()` the rule prescribes that pyatlan does not expose yet, or an SDK
seam that does not exist. Do NOT force a worse fix into the app, and do NOT
narrow a correct rule. Instead the rule-fix PR (or, when no rule change is
warranted at all, a standalone note PR against
`packages/conformance/conformance/docs/rules/<series>.md`) must carry an
**"Upstream dependency"** section stating exactly: which library/API is
missing what, the smallest upstream change that unblocks the prescribed fix,
and what the fleet should do meanwhile (usually: the finding stays visible,
unsuppressed — a tracked gap is the honest state). Put the same sentence in
your RESULT detail so the ledger and the dashboard carry it against the rule:
`rule-review:<pr-url> blocked upstream: <one line>`. A gap nobody wrote down
is a gap that gets rediscovered from scratch every sweep.

Then `cd` back to `/workspace/$(basename "$REPO")`. **Never** commit SDK changes
into the app repo, or app changes into the SDK — two clones, two branches, no
crossing.

Print: `[Stage 6/8 complete] rule-fix=<url|abstained|none-needed>`

---

## Stage 7: Deliver

Re-run the Stage 3 detect into `/tmp/remediation/after.sarif` and record the new
`atlan/summary`. If nothing survived, skip to Stage 8 with the right RESULT — do
not open an empty PR.

Stage the touched files **explicitly by path**. Never `git add -A`: the sandbox
leaves artefacts and a stray file in the diff fails the shape gate.

**`one_pr_per_rule`:**
```bash
git add <the exact paths you edited>
git commit -m "fix(conformance): resolve $RULE_ID <RuleName> ($RULE_ID)"
git push origin "$BRANCH"
# Labels first, idempotently: `gh pr create` exits non-zero on an unknown
# label, and most consumer repos have never seen these. `|| true` because a
# pre-existing label also exits non-zero, and either way the create below is
# what must not fail.
gh label create conformance-remediation --repo "$REPO" \
  --color 1D76DB --description "Opened by the conformance remediation lane (FND-18)" || true
gh pr create --repo "$REPO" --base "$BASE_REF" --head "$BRANCH" \
  --title "fix(conformance): resolve $RULE_ID <RuleName>" \
  --label conformance-remediation \
  --body-file /tmp/remediation/pr_body.md
```
Add `--label conformance-remediation:unverified` when any delivered finding was
classified `unverifiable` (lazy-create that label the same way).

**Draft delivery is keyed off the results, not the series letter:** when any
delivered result carries `deliver_as_draft: true` (the loop stamps it — today
that is the S-series under `--apply-unverifiable`), add `--draft` to
`gh pr create` AND request a named reviewer
(`gh pr edit <url> --add-reviewer "$(gh repo view "$REPO" --json owner -q .owner.login 2>/dev/null || true)"`
is NOT it — use the repo's CODEOWNERS entry for the touched paths, falling back
to the most recent human committer of the touched files:
`git log -1 --format='%ae' -- <path>`). No gate can confirm a credential
relocation resolves the same credential, so a human must press the button —
the draft state is what makes that structural rather than optional.

**`push_to_pr_branch`:** re-read the PR head first — a developer may have pushed
while you worked:
```bash
git fetch origin "$BASE_REF"
git rev-parse "origin/$BASE_REF"     # must still equal START_SHA
git push origin "HEAD:$BASE_REF"     # NEVER --force
```
If the head moved, do not push. Stop with `error: head moved during run`; the
orchestrator reschedules. Then comment the same body on `PR_NUMBER`.

### The PR/comment body must state

- rule ID, name, tier, and **`SUITE_VERSION`** (so a later reader knows which
  suite produced it)
- **`N of M` findings cleared** — honestly. A body implying the rule is done when
  the next detection still fires is worse than one that admits a remainder
- `atlan/summary.failing` before → after
- every reverted finding and why (`recheck-failed`, `orthogonal-gate-failed`,
  `cannot-verify`)
- the **decision table**: question, chosen option, evidence — every row where a
  human was flagged
- for any `unverifiable` finding: **which gate could not validate it**, in those
  words. These must never read as gate-verified

Print: `[Stage 7/8 complete] <pushed sha | no delivery>`

---

## Stage 8: Report

Emit both blocks, exactly these markers — the orchestrator parses them:

```
=== REMEDIATION SUMMARY ===
repo: atlanhq/atlan-netsuite-app
rule: L004
tier: block
suite_version: 0.20.1
findings_before: 92
findings_after: 0
cleared: 92
reverted: 0
refuted: 0
delivery: one_pr_per_rule
pr_url: https://github.com/...
main_model: <the model you ran on>
subagent_model: <the model your Task sub-agents ran on>
=== END REMEDIATION SUMMARY ===

=== DECISIONS ===
{"fingerprint":"…","question":"…","options":["…","…"],"chosen":"…","rationale":"…","evidence":["path:line"],"confidence":"high","flag_for_human":true}
=== END DECISIONS ===
```

One JSON object per line in `DECISIONS`, one per adjudicated decision. Emit the
block even when empty.

Finish with exactly one line:

```
RESULT: pushed:<sha> | exists:<url> | no-op:<reason> | rule-review:<sdk-pr-url|reason> | error:<msg>
```

Print: `[Stage 8/8 complete]`

---

### Stage 8 — batch contract (batch mode only)

In batch mode, emit ONE summary of the run and the per-unit results block in
place of the single-unit summary/RESULT:

```
=== BATCH RESULTS ===
{"repo":"atlanhq/atlan-a-app","rule":"L004","result":"pushed","pr_url":"https://github.com/…","findings_before":3,"findings_after":0,"detail":""}
{"repo":"atlanhq/atlan-a-app","rule":"E002","result":"rule-review","pr_url":"https://github.com/atlanhq/application-sdk/pull/…","detail":"detector counts re-raised broad excepts"}
{"repo":"atlanhq/atlan-b-app","rule":"L011","result":"not-reached","detail":"time budget expired"}
=== END BATCH RESULTS ===

=== DECISIONS ===
{"repo":"atlanhq/atlan-a-app","rule":"L004","fingerprint":"…","question":"…","options":["…"],"chosen":"…","rationale":"…","evidence":["path:line"],"confidence":"high","flag_for_human":true}
=== END DECISIONS ===
```

Rules:

- One JSON object per line; **every manifest unit appears exactly once**,
  including the `not-reached` ones. A unit you never mention is treated as a
  failure by the orchestrator — silence is an omission, not an outcome.
- `pushed`/`refreshed` MUST carry `pr_url` and honest `findings_before`/
  `findings_after` from the pinned re-detect — the orchestrator cross-checks
  them and disbelieves a delivery whose numbers did not drop.
- Decisions carry `repo` + `rule` so they replay to the right unit next time.
- Emit both blocks even when empty.

Finish with exactly one line:

```
RESULT: batch:<completed>/<total>
```

where `<completed>` counts units that reached ANY terminal outcome other than
`not-reached`. Print: `[Stage 8/8 complete]`
