---
name: remediate
description: Drive the conformance remediation loop (validators + OpenProse programs from the atlan-application-sdk-conformance package)
argument-hint: "[--area error-handling|logging|ci] [--rule L004[,E002]] [--strict] [--apply-unverifiable] [path]"
---

## Phase 0 — prelude (runs once, before anything else)

Pull the pinned suite and the on-disk scaffolds forward before detecting
anything. The two steps fix different things: the lock upgrade changes the
**programs and rule set** (`programs-dir` resolves inside the installed
package), the bootstrap changes the **on-disk scaffolds** (this SKILL.md, the
managed workflow shims, the vendored detect action, tests.yaml, renovate.json).
Neither substitutes for the other.

This phase is structurally outside the remediation loop and unreachable from
it — that, not a counter, is what makes re-entry impossible. It is not C002's
in-loop `bootstrap`, which is per-finding and gated by recheck.

1. **Upgrade the pin**, within the range `pyproject.toml` allows:

   ```
   uv lock --upgrade-package atlan-application-sdk-conformance
   ```

   A no-op where the package is source-pinned to a local path (`[tool.uv.sources]`
   with `path = ...`), which is the case in the SDK monorepo itself. If the lock's
   index URLs are rewritten as a side effect, revert that part of the diff.

2. **Re-sync the scaffolds**, capturing the manifest:

   ```
   uv run atlan-application-sdk-conformance bootstrap --resync --json
   ```

   `--resync` is a superset of a bare run, not an alternative to it: the
   always-overwrite set runs either way, and `--resync` adds the write-if-absent
   scaffolds (tests.yaml, renovate.json) plus the connector review kit. So it is
   always the right call here. `.gitignore` and `contract_schema.lock.json` are
   deliberately outside its scope.

3. **Read both outputs** — the JSON manifest *and* stdout:

   | Signal | Meaning | Action |
   |---|---|---|
   | `"skipped": true` | library / conformance-source repo; whole write phase no-ops | no re-read → Phase 1 |
   | a `skipped:` line on stdout | a scaffold **refused** to re-render (it declares a key the canonical cannot carry forward) | record as residue → Phase 1 |
   | `touched == []` | already canonical | no re-read → Phase 1 |
   | `touched != []` | scaffolds moved — including, possibly, this file | re-read this SKILL.md, then run step 2 **once** more |

   `touched` counts only `installed`/`updated`/`scaffolded`/`backed_up`/`removed`;
   an up-to-date file reports under `unchanged`. A per-file refusal *also* reports
   under `unchanged`, which is why stdout must be read separately — **the JSON
   alone cannot tell a refusal from convergence.**

4. **Fixpoint check** (the second run, at most). `touched == []` → converged,
   go to Phase 1. Still non-empty → **stop; do not run a third time.** Record
   `bootstrap non-idempotent on <paths>` as residue and go to Phase 1 anyway.

   **Hard cap: two bootstrap invocations, never three.** The second run is not
   belt-and-braces. Bootstrap's render kwargs come from autodetect reading back
   the files bootstrap itself writes, so run N+1's inputs are run N's outputs —
   and when a readback is not the exact inverse of its render, `touched` never
   empties. FND-361 was exactly this: `services-script` rendered bare but matched
   quoted-only, so resync deleted the live line on every single run. A second
   non-empty `touched` is that bug. It is a finding, not something to retry —
   same discipline as the loop's own oscillation detection (freeze and escalate,
   never re-attempt).

**Invariant:** after a converged Phase 0, the Phase 1 baseline should carry
**zero C002 findings**. One that appears means Phase 0 did not converge, and is
an independent check on the same property.

## Reference apps — load before any fix, verify against them after

Do not fix from memory. Every app-facing rule names a `canonical_reference`
(SARIF `atlan/canonicalReference` on the finding): a file in one of the three
maintained reference apps that already has the compliant shape. Before the
first edit of a run, make the **full checkout** of all three available:

```
mkdir -p remediation/refs
for app in atlan-mysql-app atlan-metabase-app atlan-openapi-app; do
  [ -d "remediation/refs/$app" ] || git clone --depth 1 "https://github.com/atlanhq/$app.git" "remediation/refs/$app"
done
```

`remediation/refs/` is scratch — never edited, never committed, never in a
fix's `touched_files`.

For every finding, in this order (the full contract is
`$PROGRAMS/functions/remediate-finding.prose.md`, section *Reference apps,
impact analysis and verification*):

1. **Read** the file the finding's `canonical_reference` names — the whole
   file — then grep that reference app for the same pattern, and mirror it.
   Never invent an API, kwarg or config key the reference app does not use.
2. **Analyse the impact** across the whole repo before applying: callers and
   importers, tests that pin the old behaviour, the contract and generated
   tree, `pyproject.toml`/`uv.lock`, `.env.example` and docs. Fold every
   in-scope consequence into the same edit; list the rest in `impact`.
3. **Verify** after applying and record it in `verification`: the finding is
   gone (`recheck-narrowest`), the orthogonal gate passed, a whole-series
   re-detect on the touched files introduced **no new finding for any rule**,
   and the fixed site now reads like the reference. All four true, or revert.
4. **Review the consequences** once verified: what did the fix change
   behaviourally (control flow, signatures, runtime surfaces the gates do not
   exercise, new runtime dependencies), and who is affected? Fix what is in
   scope in the same unit and re-verify; list the rest in `impact.after`. An
   empty `after` is a claim that nothing follows from the fix.
5. **A suppression is a rule-defect signal.** If the finding will not clear
   and the only way out is `# conformance: ignore[<RULE>]`, classify why:
   `site-exception` (rule is right, this site is a justified carve-out — the
   normal strict-mode suppression), `false-positive` (code matches the
   reference app, detector still flags it) or `prescription-defect` (the
   prescribed edit cannot clear it). For the last two, run
   `$PROGRAMS/functions/report-rule-defect.prose.md`: it opens a
   `fix(conformance):` PR against `atlanhq/application-sdk` with a failing
   reproducer test (and the checker/prescription fix when local), for the SDK
   owners to review. Suppress only WARN-tier findings for these reasons, and
   only citing that PR in the justification; BLOCK-tier stays in residue with
   the PR link. Never merge that PR; never edit this repo's own gate.

`autofixable = true` rules (the **auto-fixable** ruleset) are applied this
way. `autofixable = false` rules (the **migration** ruleset) are never applied
by the loop: steps 1–2 still run, and the result is a `migration_brief` in
residue — target state in the reference app, files that would change, the
external skill to run — for the connector's per-rule sub-issue.

## Phase 1+ — run the loop

Only after Phase 0 has converged (or been recorded as residue):

1. Resolve programs dir:
   - Inside a connector repo: `PROGRAMS=$(uv run atlan-application-sdk-conformance programs-dir)`
   - Anywhere else: `PROGRAMS=$(uvx atlan-application-sdk-conformance@latest programs-dir)`
2. Read `$PROGRAMS/conformance-remediation.prose.md` and execute it as the entry contract.
3. All gated re-checks call `atlan-application-sdk-conformance detect` — follow the .prose.md exactly.

## Arguments → program inputs

| Argument | Program input | Meaning |
|---|---|---|
| `--area <name>` | which area responsibilities to call | comma-separated area names; default is every enabled area |
| `--rule L004` / `--rule L004,E002` | `rule_ids` | restrict the whole run to these exact rule IDs. Pass the narrowest `series` that covers them (each ID's first letter) — the runner's `--series` matches a series *letter*, so `--series L004` activates **zero** checks; rule scoping is a post-filter on `result.rule_id`. Required to remediate one rule per run, and the only way to express "blocking tier first", since tier is per-rule not per-series |
| `--strict` | `mode = "strict"` | also remediate WARNING-tier findings; each is cleared by a real fix or a justified inline suppression |
| `--apply-unverifiable` | `apply_unverifiable = true` | let the P-, F-, I- and S-series areas apply instead of only proposing. **I-series** is genuinely gated (`docker-build`). **P-, F- and S-series gates are blind**, so their results are force-classified `unverifiable`, always routed to residue, and only accepted with a cited source for the chosen value; S-series additionally delivers as draft. Without this flag those four areas behave exactly as before |
| `<path>` | `path_prefix` | repo-root-relative prefix; post-filters which findings are remediated (the runner always scans the whole repo) |
