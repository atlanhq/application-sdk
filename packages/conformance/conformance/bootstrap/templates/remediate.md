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
