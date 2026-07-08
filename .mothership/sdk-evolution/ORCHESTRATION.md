# SDK Evolution — Orchestration Playbook

One playbook, two tiers. The dispatch prompt sets **`TIER`** (`daily` |
`weekly`). Daily is the fast, high-confidence pass (Mon–Sat). Weekly is the
Sunday superset that also does the deep, design-level and cross-repo work.

Follow every stage in order. Every finding exits in exactly one state:
**FIX** (PR), **DESIGN** (PR/ticket, `needs-design-review`), or **KILLED**
(with a reason). Nothing is deferred. Print `[Stage N complete] …` after each
stage so the run is observable in the log.

## Time budgets

| Stage | daily | weekly |
|---|---|---|
| 0 Preflight | 2 min | 3 min |
| 1 Discovery | 8 min | 20 min |
| 2 Self-verify | 4 min | 8 min |
| 3 Feasibility & worth | 4 min | 8 min |
| 4 Tickets + PRs | 12 min | 30 min |
| 5 Validation | 5 min | 10 min |
| 6 Handoff | 2 min | 3 min |
| 7 Summary | 2 min | 3 min |
| **Hard stop** | **40 min** | **110 min** |

At the hard stop: stop opening new work, close any un-validated PRs, cancel
orphan tickets, jump to Stage 7 and emit the summary. A partial run with clean
state beats a full run that dies mid-PR. (The sandbox itself caps at
`max_timeout_seconds: 7200`; the weekly hard stop sits safely inside it.)

---

## Stage 0: Preflight

Read `RUN_DATE`, `TIER`, `GHA_RUN_URL`, and (weekly only) `CONSUMER_PR_CAP` from
the prompt header — do NOT re-derive them.

1. **Working dir** — mothership cloned the repo on `main`:
   ```bash
   cd /workspace/application-sdk
   uv sync --all-extras 2>/dev/null &      # warm deps in background
   echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
   ```
2. **Read the playbook assets** (source of truth):
   - `.mothership/sdk-evolution/references/check-registry.md` — which checks
     run at this TIER, and the **DO-NOT-re-report** exclusion list.
   - `.mothership/sdk-evolution/agents/*.md` — the three discovery agents.
   - `.mothership/sdk-evolution/tools.md` — Linear + GitHub + `@sdk-review`.
3. **Build the suppression list** — query Linear for open tickets in the
   "App SDK v3.0" project (see tools.md). Extract file paths + rule IDs from
   each open ticket; skip re-raising them. On proxy failure → empty suppression
   (fail-open, you'll dedupe some duplicates but won't block).

4. **Outcome feedback (self-tuning, grounded in merges — not self-report):**
   Read the previous run's parent-ticket children and their final state. If a
   check category was **closed-unmerged ≥ 3 times** recently, it is noisy →
   this run, raise its confidence bar and drop its low-severity findings. If a
   category consistently **merges**, keep it as-is. This replaces the old
   self-improve-to-Linear YAML with a signal you can trust: what humans actually
   merged vs closed.

Print: `[Stage 0 complete] tier=<daily|weekly>, suppressed=<N>, noisy_categories=<list>`

---

## Stage 1: Discovery (3 agents, parallel)

Dispatch all three via the Agent tool simultaneously. Each reads the same files
and applies the checks the **check-registry** assigns to this TIER:

1. `agents/correctness-quality.md` →
   `[BUG] [DOCS] [TEST] [STALE] [MANIFEST] [LOG] [TYPES] [APICOMPAT]`
   and (weekly) `[ARCH] [FLAKY] [SMOKE]`.
2. `agents/safety.md` → `[SEC]` (always) and (weekly)
   `[PERF] [DEPDRIFT] [PERFTREND]`.
3. `agents/evolution.md` → `[CONF]` (rule proposals), and (weekly)
   `[DX] [CENTRAL] [TEMPORAL] [TOOLKIT] [EXAMPLE] [FLEET] [DOCSITE]`.

**Scope by surface, always all three:** `application_sdk/`,
`packages/conformance/`, `contract-toolkit/`. Weekly additionally runs the
`CONSUMERS` audit — see Stage 4.

**Hard rule:** before flagging, every agent checks the registry's
DO-NOT-re-report list. Anything already gated by ruff / conformance CI / codeql
/ trivy is dropped at source, not surfaced.

**Pre-filter (drop before Stage 2):**
- Matches suppression list → KILL.
- Zero-caller code + severity < high → KILL.
- Deprecated file + category ≠ security → KILL.
- Daily tier + finding needs a design debate → convert to a weekly DESIGN
  candidate note (don't PR it today), then KILL for this run.

Print: `[Stage 1 complete] <N> raw, <M> after pre-filter`

---

## Stage 2: Self-verify (replaces the old cross-model GPT gate)

The old pipeline challenged findings through a GPT-via-proxy call; that proxy
was the #1 source of stream-drops and false confidence. Instead, verify
in-model with an adversarial re-read.

For each surviving finding, dispatch a **fresh** verification sub-agent (Agent
tool) whose only job is to REFUTE the finding. Give it the finding + the full
file. It returns `survived` or `killed` with a one-line reason.
- Default to `killed` when the verifier is not confident the defect is real.
- `[CONF]`, `[DX]`, `[CENTRAL]`, `[TEMPORAL]` proposals skip refutation (they're
  not bug claims) but still get a feasibility read in Stage 3.
- On weekly, run TWO independent refuters for anything that will raise a
  consumer PR or an ADR — a bad cross-repo PR is expensive; require both to
  fail-to-refute.

Print: `[Stage 2 complete] <N> survived, <M> killed`

---

## Stage 3: Feasibility & worth (BEFORE any ticket/PR)

For each survivor, read the full file + its callers, then classify:
- **FIX** — clean, < 50 lines, breaks no callers, a senior engineer would merge
  it as-is → Stage 4 FIX path.
- **DESIGN** — correct but larger / needs sign-off / a v3 replacement exists /
  the fix would worsen the code → Stage 4 DESIGN path (still gets a PR with the
  proposed implementation so reviewers see real code).
- **KILL** — not worth it, not feasible, or the fix is worse than the problem.

Worth checks that force a KILL: tracked by an existing TODO+ticket; runs < 10×
per workflow and only medium severity; untouched 6+ months with no open issue.

Print: `[Stage 3 complete] <N> FIX, <M> DESIGN, <K> KILLED`

---

## Stage 4: Tickets + PRs

### 4a. Refresh main
```bash
git checkout main && git pull origin main
```

### 4b. Linear parent ticket
Create `SDK Evolution (<tier>) — <RUN_DATE>` in team **Builder Experience**,
project **App SDK v3.0**, milestone **Autonomous SDK Evolution** (create if
absent). Put the GHA run link at the top of the description:
`**Run:** [logs + cost](<GHA_RUN_URL>)`. Every finding becomes a child.

### 4c. FIX PRs (atomic, one finding at a time)
```bash
git checkout main && git clean -fd
git checkout -b <TICKET_ID> origin/main
# bug/security/perf → write the failing test FIRST, then fix
uv run pre-commit run --files <changed>
uv run pytest tests/unit/ -x -q --timeout=60
git add <specific files>            # never `git add -A`
git commit -m "<type>(<scope>): <desc> [<TICKET_ID>]"
git push origin <TICKET_ID>
gh pr create --repo atlanhq/application-sdk --base main \
  --title "<type>(<scope>): <desc> [<TICKET_ID>]" \
  --label "Autonomous SDK Evolution" --body "…"
```
Any step fails → cancel the ticket, no orphans. One test-fix retry, then KILL.

**Grouping:** bug / security / perf / arch / centralization → individual PR.
docs / test-quality / stale-cleanup → one PR per category.
**No fixed PR cap** — open a PR for every finding that survives all gates. The
bound is the stage time budget + the hard stop, not an arbitrary number; if the
budget runs out, remaining survivors carry to the next run via the suppression
list. Work in priority order so the most important work lands first when the
budget is tight: security > bug > docs/test > stale > improvement > design.

### 4d. DESIGN PRs
Same mechanics, plus: title `[DESIGN] <category>: <title> [<TICKET_ID>]`,
labels `Autonomous SDK Evolution` + `needs-design-review`, and the body MUST
include **Problem / Why it matters / Approaches considered (A/B/C) /
Recommendation / How to review**. `[CONF]` proposals additionally carry the
rule **and its remediation counterpart in this same PR** + the catalog entry.

### 4e. Weekly-only work
- **TEMPORAL** → open an **ADR PR** against `docs/adr/` (prototype diff where
  feasible), `needs-design-review`.
- **CONSUMERS** → run `/audit-consumers` (read-only discovery). Raise migration
  PRs against consumer repos with `--raise-prs`, **capped at `CONSUMER_PR_CAP`**;
  list the rotated-over repos in the parent ticket so next week picks them up.
- **TOOLKIT** → changes to `contract-toolkit/` go through the
  `toolkit-feature-workflow` downstream-compat validation before the PR opens.

Print: `[Stage 4 complete] parent=<ID>, <N> FIX PRs, <M> DESIGN PRs, <C> consumer PRs`

---

## Stage 5: Validation

For each PR: wait for CI (cap: daily 3 min, weekly 5 min), then deterministic
checks — diff touches the flagged file; required test present for
bug/security/perf; diff not bloated with unrelated files. On CI failure, one
Sonnet-style fix attempt; if it persists → close PR + cancel ticket with a
clear reason. Never leave a red PR open.

Print: `[Stage 5 complete] <N> passed, <M> killed`

---

## Stage 6: Handoff — single `@sdk-review` pass

Post ONE review request per surviving PR. This is a single review pass, **not**
the auto-complete/auto-merge loop — a human decides the merge. This is
deliberate while the rebuilt pipeline earns back trust.
```bash
gh pr comment <PR> --repo atlanhq/application-sdk \
  --body "@sdk-review Please review this Autonomous SDK Evolution PR."
```
DESIGN / ADR PRs: same comment, but note "review the approach, not just the
code." Update each Linear child to "In Review" with the PR link.

Print: `[Stage 6 complete] <N> PRs sent for one review pass`

---

## Stage 7: Summary (observable output — this is the re-enable gate)

The dispatch script writes the machine summary to `GITHUB_STEP_SUMMARY`; your
job is to make the numbers and the Linear link real:

1. Update the Linear **parent** ticket with the metrics table and keep the
   `**Run:** [logs + cost](<GHA_RUN_URL>)` line at the top.
2. Emit this block to stdout verbatim (the dispatch script parses it):
   ```
   === SDK EVOLUTION SUMMARY ===
   tier: <daily|weekly>
   run_date: <RUN_DATE>
   parent_ticket: <URL>
   discovered: <N>
   killed_prefilter: <N>
   killed_verify: <N>
   killed_feasibility: <N>
   fix_prs: <N>
   design_prs: <N>
   consumer_prs: <N>
   handed_for_review: <N>
   === END SUMMARY ===
   ```
3. Consistency audit: every child ticket has a PR link + "In Review" OR
   "Canceled" + reason. No orphans. Parent reflects the aggregate.
4. Security audit: no secrets/tokens/internal URLs in any PR or ticket body.

Print: `[Stage 7 complete] parent updated, summary emitted`

---

## Principles (non-negotiable)

- **Nothing deferred.** Every finding is FIX, DESIGN, or KILLED by end of run.
- **Don't duplicate CI.** The registry's exclusion list is binding — re-raising
  a ruff/conformance/codeql finding is a bug in the run.
- **Observability is the contract.** A run that produces no Linear parent + no
  step summary is a failed run, even if it opened PRs. This is exactly why the
  cron was disabled before.
- **One review pass, human merges.** No auto-merge until trust is re-earned.
- **Respect the tier.** Daily never opens a DESIGN debate; weekly owns the deep
  and cross-repo work.
