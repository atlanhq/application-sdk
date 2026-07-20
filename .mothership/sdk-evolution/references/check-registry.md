# SDK Evolution — Check Registry

One registry, tier-mapped. Every check declares its **tier** (`daily` /
`weekly`), the **surface** it runs against, and its **exit** (FIX PR, DESIGN
PR/ticket, or a conformance-rule proposal).

> **daily** = the light Mon–Sat pass: the last-36h **commit delta** gets ALL
> daily families; today's **`FOCUS`** family additionally goes deep across all
> three surfaces. Only report what a senior engineer would fix without a
> design debate.
> **weekly** = ONE Sunday design deep-dive on **`THEME`**. Weekly does NOT run
> the daily families.

**Worked examples** (concrete BAD → GOOD + skip-when per check) live alongside
this file — each discovery agent reads its own set:
`correctness-examples.md`, `safety-examples.md`, `evolution-examples.md`. This
file is the index (what/when/exit); those are the precision anchors.

## Daily focus rotation (set by the dispatch script, override via `FOCUS`)

| Weekday | FOCUS (deep scan, all three surfaces) |
|---|---|
| Mon | `BUG` |
| Tue | `DOCS` |
| Wed | `TEST` |
| Thu | `TYPES+APICOMPAT` |
| Fri | `STALE+MANIFEST+LOG` |
| Sat | `CONF` |

`SEC` is not a focus day — it applies to **every** daily delta scan (a
security defect must never wait for its weekday).

## Weekly theme rotation (set by the dispatch script via ISO week, override via `THEME`)

| THEME | Covers (old check names) | Owning agent |
|---|---|---|
| `ARCH` | ARCH — ADR drift, dependency direction, dumping-ground files | correctness-quality |
| `TEMPORAL` | TEMPORAL — idiomatic Temporal adoption → ADR PR | evolution |
| `CONSUMERS` | CONSUMERS + BOILERPLATE + FLEET + APPHEALTH — v3 app audit | evolution |
| `TOOLKIT` | TOOLKIT + EXAMPLE + SMOKE — toolkit deep review + scaffold check | evolution |
| `DX` | DX + CENTRAL + DOCSITE — ergonomics, centralization, docs-site drift | evolution |
| `PERF` | PERF + PERFTREND + DEPDRIFT + FLAKY — hot paths, trends, dep drift, flaky tests | safety (+ correctness-quality for FLAKY) |

## Surfaces

| Surface | Path | Daily | Weekly |
|---|---|---|---|
| SDK source | `application_sdk/` | delta + FOCUS deep | as the THEME requires |
| Conformance | `packages/conformance/` | delta + FOCUS deep (CONF = rule proposals) | as the THEME requires |
| Contract toolkit | `contract-toolkit/` | delta + FOCUS deep | TOOLKIT theme: full review via `toolkit-feature-workflow` |

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

## DAILY check families

Confidence bar is high (≥ 85). If it needs a design debate, it is not a daily
FIX — note it for the matching weekly THEME and move on. Every family below
runs on the **delta scan**; the FOCUS family additionally runs deep,
everywhere.

### BUG — semantic correctness `[focus: Mon]`
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

**Exit:** small clean fix → FIX PR (write a failing test FIRST). Larger → note
for the ARCH theme.

### SEC — security `[every daily delta scan]`
Security defects logic scanners miss: credential handling flaws, multi-tenant
isolation gaps, missing validation at system boundaries, path traversal,
error-info disclosure, unsafe deserialization reachable at runtime. Always
Critical or High — never Medium.

**Exit:** FIX PR (failing test first).

### DOCS — documentation quality & staleness `[focus: Tue]`
- Docstring drift: params/returns/raises out of sync with the signature.
- Broken or non-running code examples in `docs/` and docstrings.
- Stale version refs, dead internal links, references to removed v2 symbols.
- Public symbol (in an `__init__.py __all__`) with no docstring.

**Exit:** FIX PR (docs are low-risk). Group per doc area.

### TEST — test *quality* (not existence) `[focus: Wed]`
Existence of some test is not the check — CI coverage already tracks that.
Flag tests that pass but don't protect:
- Assertion-free or vague (`assert result`, `assert x is not None`).
- Coupled to implementation instead of behaviour.
- Missing edge-case / error-path coverage on **critical** modules.
- Real external calls in a unit test (should use in-memory mocks).
- Missing `clean_app_registry` fixture where an App subclass is defined.

**Exit:** FIX PR. Group per module.

### TYPES — public-surface type safety `[focus: Thu]`
Gradual-typing erosion pyright doesn't error on: a public/exported signature
that newly widens to `Any`, bare `dict`/`list`, or `Dict[str, Any]` in a
contract, or a missing return annotation on an exported symbol.

**Exit:** FIX PR.

### APICOMPAT — public API break detection `[focus: Thu]`
An exported symbol (in an `__all__`) removed, renamed, or with a changed
signature and **no deprecation path** — a silent break for connectors. Diff the
public surface against the last release tag.

**Exit:** FIX PR (restore + deprecate) — or note for the ARCH theme if the
break is intended.

### STALE — deprecation / dead-code / TODO aging `[focus: Fri]`
- `warnings.warn(..., DeprecationWarning)` older than 3 months where all
  callers have migrated → remove the shim.
- Zero-caller functions/classes (skip if exported, used in tests, or in
  `_temporal`/`_dapr`/`_redis` internals).
- TODO/FIXME/HACK older than 6 months with no linked ticket → file or fix.

**Exit:** small removal → FIX PR; broad cleanup → note for the ARCH theme.

### MANIFEST — capability-manifest content freshness `[focus: Fri]`
Presence is CI-gated; **content drift** is not. If a public symbol's signature
or contract changed but `docs/agents/sdk-capabilities.md` still shows the old
shape, regenerate via `/capability-manifest`.

**Exit:** FIX PR (regeneration only).

### LOG — observability signal quality `[focus: Fri]`
Not the L-series log-*level* lint (conformance owns that). Flag log **signal**
defects: exceptions swallowed with no log, stack traces dropped on re-raise,
ERROR used for non-failures, or INFO chatter that buries real lifecycle events.
Apply the `signal-over-noise` lens.

**Exit:** FIX PR.

### CONF — SDK-level conformance rule proposal `[focus: Sat]`
When ≥ 3 findings (this run or noted by recent runs) share one detectable
pattern that CI does **not** yet gate, propose a conformance rule instead of N
point fixes.
- Scope = `sdk` (runs against SDK source).
- **Rule + remediation ship in the SAME PR** (per the `/conformance` discipline).
- Do not duplicate an existing rule — check `packages/conformance` first.

**Exit:** one DESIGN PR (rule + remediation + catalog entry), `needs-design-review`.

---

## WEEKLY themes — what "deep" means per theme

The weekly output contract: **one** DESIGN PR/ADR (+ child ticket,
`needs-design-review`) + at most 3 incidental FIX PRs. Pick the single most
valuable design change the theme surfaces; fold or KILL the rest.

### ARCH — architecture / ADR drift
Check against the ADR library in `docs/adr/`:
- Dependency direction `app/ → execution/ → infrastructure/` (never reverse).
- Direct `temporalio` / `DaprClient` imports outside the `_temporal`/`_dapr`/`_redis` seams.
- Single Input / single Output per method; additive-only contract evolution.
- Design coherence; **dumping-ground** files (many unrelated concerns).
- **Cross-cutting refactor detection:** a single file producing ≥ 5 findings →
  ONE holistic DESIGN ticket, not five point fixes.

### TEMPORAL — better Temporal-concept adoption → ADR PR
Where the SDK could model workflows more idiomatically:
- Signals / queries / updates instead of polling or side-channel state.
- Child workflows / `continue-as-new` for unbounded or long histories.
- Correct heartbeat + activity retry-policy defaults.
- Tightening determinism boundaries.
The DESIGN output is an **ADR PR** against `docs/adr/` with a prototype diff
where feasible.

### CONSUMERS — v3 app audit (+ fleet health & version drift)
Run `/audit-consumers --sdk-major 3` (grep-based, read-only discovery) across
`atlanhq/` **v3 apps only**. Four angles:
- **Boilerplate removal** — app code that reimplements something the SDK
  ALREADY provides → migration PR that deletes it and calls the SDK.
- **Missing guardrail → new check** — recurring reinvention no rule catches →
  an **app-scope conformance rule** (rule + remediation same PR).
- **SDK gap → evolution** — a pattern ≥ 3 apps reinvent that the SDK does NOT
  provide → this week's DESIGN proposal.
- **APPHEALTH / FLEET** — per-app build/boot/CI status against the current SDK
  and version-drift laggards → a fleet-health section on the parent ticket.
Migration PRs use `--raise-prs`, capped at `CONSUMER_PR_CAP`; rotate the
remainder to the next CONSUMERS week.

### TOOLKIT — contract-toolkit deep review
Review `contract-toolkit/` through the `toolkit-feature-workflow` lens:
classify affected generated surfaces and run the **mandatory downstream-compat
validation** before proposing changes. Never review it like plain SDK source.
Include: `contract-toolkit/examples/` freshness (EXAMPLE) and the
`/scaffold-app` golden-path boot check (SMOKE).

### DX — ergonomics, centralization & docs-site drift
- Repeated boilerplate in ≥ 3 places → propose one SDK abstraction (CENTRAL).
- Confusing param names, > 5 required params, inconsistent sibling APIs.
- Missing convenience/batch methods connectors keep reimplementing.
- docs.atlan.com SDK guides referencing removed/renamed symbols (DOCSITE, via
  the `write-docs` lens).

### PERF — performance, trends, dependencies & flaky tests
- Hot-path review: blocking-in-async, missing timeouts, unbounded memory, N+1,
  missing pooling, sync large-file IO. Only worth-checked hot paths — skip
  low-frequency / zero-caller code (that worth-check lives in
  `safety-examples.md`).
- PERFTREND: run the micro-benchmark suite vs the previous PERF week; flag
  regressions over a threshold instead of one-shot guesses.
- DEPDRIFT: direct deps and the **Dapr**/**Temporal** SDK versions materially
  behind upstream stable, or a pin blocking a security-relevant upgrade
  (beyond CVE scanning, which trivy/grype own).
- FLAKY: mine recent CI history for tests that pass only on retry.
