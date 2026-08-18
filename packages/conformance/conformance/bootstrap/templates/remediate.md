---
name: remediate
description: Drive the conformance remediation loop (validators + OpenProse programs from the atlan-application-sdk-conformance package)
argument-hint: "[--area error-handling|logging|ci] [--rule L004[,E002]] [--strict] [--apply-unverifiable] [path]"
---

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
| `--apply-unverifiable` | `apply_unverifiable = true` | let the P-, I- and S-series areas apply instead of only proposing. **I-series** is genuinely gated (`docker-build`). **P- and S-series gates are blind**, so their results are force-classified `unverifiable`, always routed to residue, and only accepted with a cited source for the chosen value; S-series additionally delivers as draft. Without this flag those three areas behave exactly as before |
| `<path>` | `path_prefix` | repo-root-relative prefix; post-filters which findings are remediated (the runner always scans the whole repo) |
