---
kind: responsibility
name: conformance-remediation
description: >
  Top-level conformance remediation entry point.  Subscribes to every
  per-area violations facet and is clean only when all subscribed areas are
  clean.  Delegates the bounded gated loop to each area responsibility.
---

### Maintains

The current remediation state of the working tree across all enabled
conformance rule areas (error-handling, logging, CI, prescriptions,
optimizations, dependency, deprecation, dockerfile, tests, contract-toolkit,
security).

#### violations-summary

An aggregate of violation counts across all enabled areas:

```
{
  "failing": <count of unsuppressed FAILING results>,
  "warning": <count of unsuppressed WARNING results>,
  "suppressed": <count of suppressed results (audit trail)>,
  "residue": <count of findings routed to human review>
}
```

Postcondition (deterministic validator — never render-attested):

**Default mode:** `atlan-application-sdk-conformance detect --repo . --series E,L,C,P,O,D,B,I,T,K,S` exits 0 — zero unsuppressed FAILING results across all enabled areas.

**Strict mode** (`--strict`): additionally, the `atlan/summary.warning` count
in the SARIF output is 0 — zero unsuppressed WARNING results.  Every WARNING
was cleared by a real fix or by a justified inline suppression.

#### residue

The set of findings that could not be auto-resolved this run, together with
the reason each was routed here:

- `judgment` — the model made a non-trivial fix; route to human for review
  before merge.
- `suppressed` — the model proposed a `# conformance: ignore` directive for a
  WARNING (strict mode); route to human to confirm the justification is sound.
- `recheck-failed` — the proposed edit did not clear the finding; manual fix
  needed.
- `orthogonal-gate-failed` — the edit cleared the finding but broke tests.
- `not-remediable` — no prescription exists for this area yet.
- `oscillation` — the loop detected a repeating violation-set and froze.
- `max-attempts` — the cap was reached with violations remaining.
- `unverifiable` — the fix was applied and both gates passed, but for this area
  the gates are structurally blind (P- and S-series under
  `apply_unverifiable`), so passing them proved nothing.  Always routed here;
  the human review *is* the gate.  Distinct from `judgment`, which means the fix
  was verified but non-trivial.
- `no-cited-evidence` — a blind-gate area proposed a fix without citing a source
  for the value it chose, so it was never applied.

### Inputs

- `scope` — repository root path.
- `mode` — `"default"` (FAILING only) or `"strict"` (FAILING + WARNING).
- `rule_ids` — optional list of exact rule IDs to restrict this run to, e.g.
  `["L004"]`.  Forwarded to every area and through to `detect-violations`,
  which pushes them down to the runner natively as `--rule` when the pinned
  runner accepts the flag (a runner that rejects it falls back to series +
  post-filter — see `functions/detect-violations.prose.md`).  Omitted ⇒ every rule in the enabled
  series.  This is what lets a caller remediate exactly one rule per run, which
  is also the only way to express "blocking tier first": tier is a **per-rule**
  property, so it cannot be selected through `series`.
- `apply_unverifiable` — boolean, default `false`.  When `false`, the P-, I- and
  S-series areas behave exactly as before: propose, never apply.  When `true`,
  they apply through the full gated loop.  For the I-series that is now a
  genuinely verified fix (the `docker-build` gate exists); for P and S the gates
  remain blind, so those results are force-classified `"unverifiable"` and always
  land in residue.  Opt-in precisely because the caller is accepting review
  responsibility that a gate cannot discharge.

### Requires

- `violations-error-handling` from `error-handling-area`
- `violations-logging` from `logging-area`
- `violations-ci` from `ci-area`
- `violations-prescriptions` from `prescriptions-area`
- `violations-optimizations` from `optimizations-area`
- `violations-dependency` from `dependency-area`
- `violations-deprecation` from `deprecation-area`
- `violations-dockerfile` from `dockerfile-area`
- `violations-tests` from `tests-area`
- `violations-contract-toolkit` from `contract-toolkit-area`
- `violations-security` from `security-area`

Forme auto-wires these subscriptions from the matching `#### facet` names in
the area responsibilities.  This node is clean only when every subscribed
facet is clean.

### What correct looks like

Before editing anything, read the rule's own **What correct looks like** block in
`docs/rules/<series>.md`. Getting these wrong is the most common way a
remediation makes a repo worse:

- **Compliant example** — every `app`- and `both`-scoped rule names a file in a
  maintained reference app (`atlan-hello-world-app`, `atlan-openapi-app`,
  `atlan-mysql-app`, `atlan-metabase-app`) that already has the shape you are
  trying to reach. Open it. Copy from those, never from an arbitrary connector:
  a connector may be mid-migration and is not a model of anything. Several of
  these blocks also name the *suppression* the reference app carries, which is
  what a legitimate carve-out looks like when one exists.
- **Interacts with** — the other rule or gate that constrains this fix. Some
  obvious remedies are illegal: a second rule forbids the edit, or an
  append-only guard refuses it. Check before you spend the attempt.
- **Already correct when** — what a settled carve-out looks like. For a
  suppress-only rule a justified inline directive IS the fix; re-"fixing" it
  every cycle is churn, not progress.

**Where the fix goes.** By default it goes in the hand-written source of the
repo you are scanning — which is what `Scope` already told you, so most rules
say nothing further. A rule whose fix lands somewhere else says so explicitly,
as *Fix belongs in* in the same doc:

| locus | edit here |
|---|---|
| `contract` | `contract/*.pkl`, then the repo's OWN generate task — never the generated output, and never a bare `pkl eval`, which skips post-processing and rewrites unrelated files |
| `toolkit` | the `contract-toolkit` renderer; **no app-side change can resolve it** |
| `ci` | `.github/**` |
| `packaging` | `pyproject.toml`, `uv.lock`, `Dockerfile`, `atlan.yaml` |
| `tests` | the app's own test suite |
| `app` / `sdk` | only ever shown when it contradicts the scope — a rule that runs on one surface and is fixed on the other |

So the field is worth reading precisely because it is usually absent. A finding
that declares a locus, which you are about to fix by editing app source, is a
finding you have misread. Stop and re-read the block above.

`test_app_facing_rules_name_a_canonical_reference` keeps the compliant example
present on every rule that can reach a consumer repo, and
`test_non_app_loci_explain_themselves` keeps guidance on any BLOCK rule the app
cannot fix on its own.

### Continuity

Input-driven: re-render when any `*.py` file or `.github/` file under `scope`
changes.  In the Reactor-ready path this is the filesystem watch source; in
the Claude Code skill path the skill caller drives re-invocation on demand.

### Execution

```prose
parallel:
  call error-handling-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call logging-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call ci-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call prescriptions-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
    apply_unverifiable: apply_unverifiable
  call optimizations-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call dependency-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call deprecation-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call dockerfile-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
    apply_unverifiable: apply_unverifiable
  call tests-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call contract-toolkit-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
  call security-area
    scope: scope
    mode: mode
    rule_ids: rule_ids
    apply_unverifiable: apply_unverifiable

# Collect residue from all areas and emit the unified report.
emit violations-summary and residue
```
