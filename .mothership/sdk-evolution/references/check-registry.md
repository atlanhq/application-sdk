# SDK Evolution — Check Registry

One registry, tier-mapped. This replaces the old per-domain rule files. Every
check declares its **tier** (`daily` / `weekly`), the **surface** it runs
against, and its **exit** (FIX PR, DESIGN PR/ticket, or a conformance-rule
proposal).

> **daily** = the fast, high-confidence pass that runs Mon–Sat. Whole SDK, but
> shallow: only report what a senior engineer would fix without a design debate.
> **weekly** = the Sunday superset. Everything daily does, plus the deep,
> design-level and cross-repo work. Weekly is the expensive run.

**Worked examples** (concrete BAD → GOOD + skip-when per check) live alongside
this file — each discovery agent reads its own set:
`correctness-examples.md`, `safety-examples.md`, `evolution-examples.md`. This
file is the index (what/when/exit); those are the precision anchors.

## Surfaces (all three, every run)

| Surface | Path | Daily depth | Weekly depth |
|---|---|---|---|
| SDK source | `application_sdk/` | full, shallow | full, deep |
| Conformance | `packages/conformance/` | rule-proposal only | rule-proposal + `/remediate` |
| Contract toolkit | `contract-toolkit/` | lint + docstring drift | full review via `toolkit-feature-workflow` (downstream-compat) |

---

## DO NOT re-report — already gated by CI

The pipeline's value is the semantic and design layer that static gates
**cannot** reach. Never open a PR/ticket for anything below — it is already a
required check and a duplicate finding just burns trust and cost.

| Already enforced | By |
|---|---|
| import ordering, star/unused imports, naming, line length | `ruff` / pre-commit |
| f-strings in logs (G004), `%`/`+` in logs (G003/G001), `print` (T201) | `ruff` / conformance L-series |
| logging levels, secret hygiene, no-raw-HTTP-to-Atlan, metadata rules | conformance E/L/S/P/O series (`conformance-ci.yaml`) |
| generated-artifact freshness (manifests, apidocs) | conformance K-series + `generated-freshness.yaml` |
| dependency CVEs, SQL/command-injection static patterns | `codeql`, `trivy`, `grype`, `daily-security-scan` |
| capability-manifest *presence* | `capability-manifest-check.yaml` |

If discovery keeps surfacing a class of issue that *isn't* yet gated, that is
not a PR — it is a **conformance-rule proposal** (check `CONF` below).

---

## DAILY checks

Confidence bar is high (≥ 85). If it needs a design debate, it is not a daily
FIX — downgrade it to a weekly DESIGN candidate and move on.

### BUG — semantic correctness `[daily]`
Runtime-correctness defects static analysis misses. Priority order:
1. Temporal **determinism** violations — `random`/`time`/wall-clock/IO inside
   `run()` / `@entrypoint` (must be in a `@task`).
2. Missing **heartbeats** in long-running `@task` methods.
3. **Blocking** calls in async without `self.run_in_thread()`.
4. Race conditions on shared mutable state across coroutines.
5. Resource leaks (unclosed connections / file handles / executors).
6. Swallowed exceptions hiding real failures; missing `from e` chaining.
7. None/Optional mishandling; mutable default arguments.
8. Off-by-one, inverted conditions, wrong comparison operators.

**Exit:** small clean fix → FIX PR (write a failing test FIRST). Larger → weekly DESIGN.

### DOCS — documentation quality & staleness `[daily]`
- Docstring drift: params/returns/raises out of sync with the signature.
- Broken or non-running code examples in `docs/` and docstrings.
- Stale version refs, dead internal links, references to removed v2 symbols.
- Public symbol (in an `__init__.py __all__`) with no docstring.

**Exit:** FIX PR (docs are low-risk). Group per doc area.

### TEST — test *quality* (not existence) `[daily]`
Existence of some test is not the check — CI coverage already tracks that.
Flag tests that pass but don't protect:
- Assertion-free or vague (`assert result`, `assert x is not None`).
- Coupled to implementation instead of behaviour.
- Missing edge-case / error-path coverage on **critical** modules.
- Real external calls in a unit test (should use in-memory mocks).
- Missing `clean_app_registry` fixture where an App subclass is defined.

**Exit:** FIX PR. Group per module.

### STALE — deprecation / dead-code / TODO aging `[daily]`
- `warnings.warn(..., DeprecationWarning)` older than 3 months where all
  callers have migrated → remove the shim.
- Zero-caller functions/classes (skip if exported, used in tests, or in
  `_temporal`/`_dapr`/`_redis` internals).
- TODO/FIXME/HACK older than 6 months with no linked ticket → file or fix.

**Exit:** small removal → FIX PR; broad cleanup → weekly DESIGN.

### MANIFEST — capability-manifest content freshness `[daily]`
Presence is CI-gated; **content drift** is not. If a public symbol's signature
or contract changed but `docs/agents/sdk-capabilities.md` still shows the old
shape, regenerate via `/capability-manifest`.

**Exit:** FIX PR (regeneration only).

### LOG — observability signal quality `[daily]`
Not the L-series log-*level* lint (conformance owns that). Flag log **signal**
defects: exceptions swallowed with no log, stack traces dropped on re-raise,
ERROR used for non-failures, or INFO chatter that buries real lifecycle events.
Apply the `signal-over-noise` lens.

**Exit:** FIX PR.

### TYPES — public-surface type safety `[daily]`
Gradual-typing erosion pyright doesn't error on: a public/exported signature
that newly widens to `Any`, bare `dict`/`list`, or `Dict[str, Any]` in a
contract, or a missing return annotation on an exported symbol.

**Exit:** FIX PR.

### APICOMPAT — public API break detection `[daily]`
An exported symbol (in an `__all__`) removed, renamed, or with a changed
signature and **no deprecation path** — a silent break for connectors. Diff the
public surface against the last release tag.

**Exit:** FIX PR (restore + deprecate) — or DESIGN if the break is intended.

### CONF — SDK-level conformance rule proposal `[daily]`
When ≥ 3 findings this run share one detectable pattern that CI does **not**
yet gate, propose a conformance rule instead of N point fixes.
- Scope = `sdk` (runs against SDK source).
- **Rule + remediation ship in the SAME PR** (per the `/conformance` discipline).
- Do not duplicate an existing rule — check `packages/conformance` first.

**Exit:** one DESIGN PR (rule + remediation + catalog entry), `needs-design-review`.

---

## WEEKLY checks (superset — daily + the below)

Budget is larger; fan-out and cross-repo work live here.

### ARCH — architecture / ADR drift `[weekly]`
Check against the ADR library in `docs/adr/`:
- Dependency direction `app/ → execution/ → infrastructure/` (never reverse).
- Direct `temporalio` / `DaprClient` imports outside the `_temporal`/`_dapr`/`_redis` seams.
- Single Input / single Output per method; additive-only contract evolution.
- Design coherence; **dumping-ground** files (many unrelated concerns).
- **Cross-cutting refactor detection:** a single file producing ≥ 5 findings →
  ONE holistic DESIGN ticket, not five point fixes.

**Exit:** DESIGN PR/ticket, `needs-design-review`.

### TEMPORAL — better Temporal-concept adoption → ADR PR `[weekly]`
Where the SDK could model workflows more idiomatically:
- Signals / queries / updates instead of polling or side-channel state.
- Child workflows / `continue-as-new` for unbounded or long histories.
- Correct heartbeat + activity retry-policy defaults.
- Tightening determinism boundaries.

**Exit:** an **ADR PR** against `docs/adr/` proposing the change, with a
prototype diff where feasible. `needs-design-review`.

### CONSUMERS — v3 app audit → evolution + boilerplate removal + conformance `[weekly]`
Run `/audit-consumers --sdk-major 3` (grep-based, read-only discovery) across
`atlanhq/` **v3 apps only** (v2 / other majors are skipped). Four angles:
- **Boilerplate removal** — app code that reimplements something the SDK
  ALREADY provides (retry/backoff, pagination, credential resolution,
  state/object store, logging setup, heartbeating, typed errors, config
  parsing, …). Raise a migration PR that **deletes the app code and calls the
  SDK** instead — smaller app surface, one canonical path.
- **Missing guardrail → new check** — when the same reinvention recurs and no
  rule catches it, add an **app-scope conformance rule** (scope = `app`, rule +
  remediation same PR) so future apps can't drift back into it.
- **SDK gap → evolution** — a pattern ≥ 3 apps reinvent that the SDK does NOT
  yet provide → DESIGN proposal for a new SDK utility.
- **Migration PRs** raised against app repos with `--raise-prs`, limited by
  `CONSUMER_PR_CAP` per run (external-repo safety knob for the 2h budget);
  rotate the remainder to following weeks.

**Exit:** SDK DESIGN tickets + app conformance PR(s) + boilerplate-removal PRs
(≤ cap) against v3 app repos.

### APPHEALTH — v3 app fleet health `[weekly]`
Are the v3 apps actually running? For each discovered **v3** app, check whether
it still **builds / boots / passes its own CI against the CURRENT SDK** — so a
broken or stalled fleet is visible instead of silent. Surface apps that are red,
or pinned to an old SDK major/minor and drifting.

**Exit:** a fleet-health section on the parent ticket + a DESIGN ticket per app
that is broken against the current SDK.

### TOOLKIT — contract-toolkit deep review `[weekly]`
Review `contract-toolkit/` through the `toolkit-feature-workflow` lens:
classify affected generated surfaces and run the **mandatory downstream-compat
validation** before proposing changes. Never review it like plain SDK source.

**Exit:** FIX PR for safe changes; DESIGN for anything touching generated contracts.

### DX / CENTRAL — ergonomics & centralization `[weekly]`
- Repeated boilerplate in ≥ 3 places → propose one SDK abstraction.
- Confusing param names, > 5 required params, inconsistent sibling APIs.
- Missing convenience/batch methods connectors keep reimplementing.

**Exit:** DESIGN PR with proposed implementation.

### PERF — performance review `[weekly]`
Only worth-checked hot paths (skip low-frequency / zero-caller code):
blocking-in-async on hot paths, missing timeouts, unbounded memory, N+1,
missing pooling, sync large-file IO. Static perf lint is not the job here.

**Exit:** FIX PR (with a benchmark note) or DESIGN.

### EXAMPLE — example-app freshness `[weekly]`
`contract-toolkit/examples/` still builds and reflects current SDK APIs; no
references to removed symbols.

**Exit:** FIX PR.

### FLEET — v3 adoption & version drift `[weekly]`
From the `/audit-consumers` discovery data, which `atlanhq/` v3 apps are N SDK
versions behind. Surface the laggards so adoption is visible; complements the
conformance fleet dashboard.

**Exit:** a summary section on the parent ticket (+ a DESIGN ticket where a
migration is actually needed).

### DEPDRIFT — dependency / runtime version staleness `[weekly]`
Beyond CVE scanning (trivy/grype own that): direct deps, and the **Dapr** and
**Temporal** SDK versions, materially behind upstream stable — or a pin that is
blocking a security-relevant upgrade.

**Exit:** bump PR — or DESIGN if the upgrade needs source changes.

### PERFTREND — performance regression trend `[weekly]`
Run the micro-benchmark suite and compare against the previous weekly run; flag
regressions over a threshold instead of one-shot guesses. Static perf lint is
not the job (that's the `PERF` check).

**Exit:** DESIGN ticket with the measured delta.

### FLAKY — flaky-test detection `[weekly]`
Mine recent CI history for tests that pass only on retry or intermittently —
the unit suite hides these because a green re-run looks clean.

**Exit:** FIX PR (stabilise) — or DESIGN if it exposes a real race.

### SMOKE — golden-path scaffold check `[weekly]`
Does `/scaffold-app` still produce an app that boots against the current SDK?
Catches integration breakage the unit suite misses.

**Exit:** FIX PR.

### DOCSITE — published-docs drift `[weekly]`
Cross-check the docs.atlan.com SDK guides against the real APIs (via the
`write-docs` lens); flag guides that reference removed/renamed symbols.

**Exit:** DESIGN ticket (published-docs changes are reviewed).
