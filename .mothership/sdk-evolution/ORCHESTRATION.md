# SDK Evolution — Orchestration Playbook

One playbook, two tiers. The dispatch prompt sets **`TIER`** (`daily` |
`weekly`) plus the tier's scope knob:

- **daily** (Mon–Sat) — the light pass. Two scans only: the **commit delta**
  of the last 36 hours across all daily check families, and today's
  **`FOCUS`** family deep across all three surfaces. Quiet day → cheap early
  exit.
- **weekly** (Sunday) — ONE design deep-dive on **`THEME`**. The output is a
  single well-argued DESIGN PR/ADR, not a breadth sweep. Weekly does NOT run
  the daily families.

Follow every stage in order. Every finding exits in exactly one state:
**FIX** (PR), **DESIGN** (PR/ticket, `needs-design-review`), or **KILLED**
(with a reason). Nothing is deferred. Print `[Stage N complete] …` after each
stage so the run is observable in the log.

## Time budgets

| Stage | daily | weekly |
|---|---|---|
| 0 Preflight | 2 min | 3 min |
| 1 Discovery | 6 min | 15 min |
| 2 Self-verify | 3 min | 6 min |
| 3 Feasibility & worth | 3 min | 6 min |
| 4 Tickets + PRs | 6 min | 20 min |
| 5 Validation | 3 min | 6 min |
| 6 Handoff | 1 min | 2 min |
| 7 Summary | 1 min | 2 min |
| **Hard stop** | **25 min** | **60 min** (CONSUMERS theme: **90 min**) |

At the hard stop: stop opening new work, close any un-validated PRs, cancel
orphan tickets, jump to Stage 7 and emit the summary. A partial run with clean
state beats a full run that dies mid-PR. (The sandbox itself caps at
`max_timeout_seconds: 7200`; both hard stops sit safely inside it.)

---

## Stage 0: Preflight

Read `RUN_DATE`, `TIER`, `GHA_RUN_URL`, plus `FOCUS` (daily) or `THEME`
(weekly, with `CONSUMER_PR_CAP` on the CONSUMERS theme) from the prompt
header — do NOT re-derive them.

1. **Working dir** — mothership cloned the repo:
   ```bash
   cd /workspace/application-sdk
   uv sync --all-extras 2>/dev/null &      # warm deps in background
   echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null
   ```
2. **Read the playbook assets** (source of truth):
   - `.mothership/sdk-evolution/references/check-registry.md` — the check
     families, their focus-day/theme mapping, and the **DO-NOT-re-report**
     exclusion list.
   - `.mothership/sdk-evolution/agents/*.md` — the three discovery agents.
   - `.mothership/sdk-evolution/tools.md` — Linear + GitHub + `@sdk-review`
     + the completion-marker issue.
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

Print: `[Stage 0 complete] tier=<daily|weekly>, focus/theme=<value>, suppressed=<N>, noisy_categories=<list>`

---

## Stage 1: Discovery

### Daily — two scans, nothing else

1. **Delta scan.** Compute the change window:
   ```bash
   git log --since='36 hours ago' --name-only --pretty=format: origin/main | sort -u
   ```
   Every changed file on the three surfaces gets ALL daily check families
   (see the registry). Dispatch only the discovery agents that own a family
   with delta files to look at; give each agent the file list.
2. **Focus scan.** Today's `FOCUS` family (or families, e.g.
   `STALE+MANIFEST+LOG`) is scanned **deep across all three surfaces**
   (`application_sdk/`, `packages/conformance/`, `contract-toolkit/`) by the
   agent that owns it.
3. **Early exit.** Empty delta AND clean focus scan → print
   `[Stage 1 complete] 0 raw — early exit` and jump straight to Stage 7
   (all-zero summary, marker comment, done). Do not pad the run.

### Weekly — the theme, only the theme

Dispatch ONLY the agent(s) that own this week's `THEME` (registry has the
mapping). The goal is depth: read the whole subsystem, its callers, its ADRs,
its consumers — enough to argue one design change convincingly. Do NOT run
the daily families; incidental small defects you trip over may exit as at
most 3 FIX PRs, everything else folds into the theme investigation or is
KILLED.

**Hard rule (both tiers):** before flagging, every agent checks the registry's
DO-NOT-re-report list. Anything already gated by ruff / conformance CI / codeql
/ trivy is dropped at source, not surfaced.

**Pre-filter (drop before Stage 2):**
- Matches suppression list → KILL.
- Zero-caller code + severity < high → KILL.
- Deprecated file + category ≠ security → KILL.
- Daily tier + finding needs a design debate → note it as a candidate for the
  matching weekly THEME (mention it on the parent ticket if one is created),
  then KILL for this run.

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
  proposed implementation so reviewers see real code). Daily never opens
  DESIGN work — note it for the matching weekly theme and KILL.
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
(Verification runs may have cloned a non-main branch; PRs always branch off
`origin/main`.)

### 4b. Linear parent ticket

**Zero survivors → no parent ticket.** A run with nothing to hand over leaves
only the Stage 7 summary + marker comment — daily Linear noise is a cost, not
observability.

Otherwise create `SDK Evolution (<tier>) — <RUN_DATE>` in team
**Builder Experience**, project **App SDK v3.0**, milestone
**Autonomous SDK Evolution** (create if absent). Put the GHA run link at the
top of the description: `**Run:** [logs + cost](<GHA_RUN_URL>)`. Every finding
becomes a child.

**Tag the reviewers** (see tools.md → Reviewers): add both **Vaibhav Chopra**
and **Chris** as subscribers on this parent and @-mention both in an opening
comment, so they get one ping per run.

### 4c. FIX PRs (atomic, one finding at a time)

**Ticket first, then PR.** For each finding: create a **child Linear ticket**
under the parent (this yields `<TICKET_ID>`), then branch off it. The ticket
always precedes the PR — the branch and PR are named after the ticket, and if
any step below fails the ticket is cancelled (no orphaned tickets, ever).

```bash
# 0. create child ticket under the parent → TICKET_ID (see tools.md)
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

**Grouping:** bug / security / perf → individual PR.
docs / test-quality / stale-cleanup → one PR per category.
**Daily bound** is the time budget; work in priority order so the most
important work lands first: security > bug > docs/test > stale > improvement.
**Weekly bound** is the output contract: ONE DESIGN PR/ADR + ≤ 3 incidental
FIX PRs.

### 4d. DESIGN PR (weekly theme output)
Same mechanics, plus: labels `Autonomous SDK Evolution` +
`needs-design-review` (the label marks DESIGN status — NOT the title), and the
body MUST include **Problem / Why it matters / Approaches considered (A/B/C) /
Recommendation / How to review**. The title must satisfy the repo's
`Validate PR title` gate (Conventional Commits + path scoping): docs/ADR-only
proposals → `chore(adr): <title> [<TICKET_ID>]`; proposals carrying SDK source
prototype code → `feat(<scope>): …` / `fix(<scope>): …`. `[CONF]` proposals additionally carry the
rule **and its remediation counterpart in this same PR** + the catalog entry.

**Tag the reviewers on every `needs-design-review` ticket** (see tools.md →
Reviewers): add both **Vaibhav Chopra** and **Chris** as subscribers and
@-mention both in a comment asking them to review the approach. This applies
to DESIGN and ADR tickets — anything needing human sign-off. Routine FIX
tickets are NOT tagged (they go to `@sdk-review`).

### 4e. Theme-specific playbooks (weekly)
- **TEMPORAL** → the DESIGN output is an **ADR PR** against `docs/adr/`
  (prototype diff where feasible), `needs-design-review`.
- **CONSUMERS** → run `/audit-consumers --sdk-major 3` (**v3 apps only**).
  Cover: (a) **boilerplate removal** — app code reimplementing SDK-provided
  functionality → migration PR that deletes it and calls the SDK; (b) **missing
  guardrails** → new app-scope conformance rule (rule + remediation same PR);
  (c) **SDK gaps** → the week's DESIGN ticket for a new SDK utility;
  (d) **APPHEALTH/FLEET** — fleet health + version-drift section on the parent
  ticket. App-repo PRs use `--raise-prs`, **capped at `CONSUMER_PR_CAP`**;
  list rotated-over repos on the parent ticket so the next CONSUMERS week
  picks them up.
- **TOOLKIT** → changes to `contract-toolkit/` go through the
  `toolkit-feature-workflow` downstream-compat validation before the PR opens;
  include the EXAMPLE freshness and `/scaffold-app` SMOKE checks.
- **ARCH / DX / PERF** → standard DESIGN PR per 4d (PERF findings need a
  benchmark note or a measured PERFTREND delta).

Print: `[Stage 4 complete] parent=<ID|none>, <N> FIX PRs, <M> DESIGN PRs, <C> consumer PRs`

---

## Stage 5: Validation

For each PR: wait for CI (cap: daily 3 min, weekly 5 min), then deterministic
checks — diff touches the flagged file; required test present for
bug/security/perf; diff not bloated with unrelated files. On CI failure, one
fix attempt; if it persists → close PR + cancel ticket with a clear reason.
Never leave a red PR open.

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
job is to make the numbers real AND durable:

1. If a parent ticket exists, update it with the metrics table and keep the
   `**Run:** [logs + cost](<GHA_RUN_URL>)` line at the top.
2. Emit this block to stdout verbatim (the dispatch script parses it).
   `parent_ticket` is `none` on a zero-survivor run:
   ```
   === SDK EVOLUTION SUMMARY ===
   tier: <daily|weekly>
   run_date: <RUN_DATE>
   parent_ticket: <URL|none>
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
3. **Post the completion marker** (the stream-drop backstop — see tools.md →
   Completion marker). Find the pinned tracking issue by label and comment the
   marker line + the SAME summary block:
   ```bash
   ISSUE=$(gh issue list --repo atlanhq/application-sdk \
     --label sdk-evolution-marker --state all --limit 1 \
     --json number --jq '.[0].number')
   # First run ever: create it (label + issue) if $ISSUE is empty — see tools.md.
   gh issue comment "$ISSUE" --repo atlanhq/application-sdk \
     --body "marker: sdk-evolution-<TIER>-<RUN_DATE>

   <the summary block verbatim>"
   ```
   If mothership's SSE stream to GitHub Actions drops mid-run, this comment is
   how the workflow still learns the run completed. **Never skip it.**
4. Consistency audit: every child ticket has a PR link + "In Review" OR
   "Canceled" + reason. No orphans. Parent reflects the aggregate.
5. Security audit: no secrets/tokens/internal URLs in any PR or ticket body.

Print: `[Stage 7 complete] parent updated, summary emitted, marker posted`

---

## Principles (non-negotiable)

- **Nothing deferred.** Every finding is FIX, DESIGN, or KILLED by end of run.
- **Don't duplicate CI.** The registry's exclusion list is binding — re-raising
  a ruff/conformance/codeql finding is a bug in the run.
- **Observability is the contract.** A run that produces no step summary and
  no completion marker is a failed run, even if it opened PRs. This is exactly
  why the cron was disabled before.
- **One review pass, human merges.** No auto-merge until trust is re-earned.
- **Respect the tier.** Daily is delta + FOCUS only and never opens a DESIGN
  debate; weekly is ONE theme, not a breadth sweep.
- **Cheap when quiet.** An empty delta and a clean focus scan is a SUCCESS,
  not a reason to dig for marginal findings.
