# SDK Review — Learned-not-to-flag log

> A running record of findings that sdk-review must NOT raise. Entries
> arrive from two sources: (1) `@sdk-review challenge` flows where
> Claude withdraws a finding and emits a retrospective YAML block; (2)
> explicit team guidance recording "do not flag this pattern".
>
> **Purpose**: teach future reviews to avoid re-flagging the same
> false-positive pattern. When running a fresh `@sdk-review`, the
> session consults this log as part of its rule set — if a candidate
> finding matches a `do_not_flag` pattern here, the finding must be
> withdrawn silently (no inline comment, no auto-fix, no removal
> request).

---

## Entries

```yaml
- pattern: "@pytest.mark.asyncio decorator on test functions"
  do_not_flag: true
  added: 2026-05-20
  source: team-guidance
  reason: |
    Whether to use the @pytest.mark.asyncio decorator alongside
    pytest's asyncio_mode="auto" is a team style preference, not a
    correctness issue. The decorator does not break anything and
    several team members prefer it for explicitness.

    Earlier guidance treated this as redundant/forbidden. That
    guidance has been retracted by the team. sdk-review must NOT
    surface it as a finding, inline comment, or auto-fix.
  applies_to:
    - "@pytest.mark.asyncio"
    - "@pytest.mark.asyncio(...)" # with parameters
    - "pytest.mark.asyncio" # in usefixtures / similar
  detection_to_skip:
    - "grep -E '@pytest\\.mark\\.asyncio' diff"
```

---

## CI-enforced patterns — do NOT duplicate (deterministic gate already blocks these)

> These patterns are each caught **and blocked** by a deterministic CI
> check that runs on every PR (ruff `extend-select`, the conformance
> suite gate, Trivy, or the coverage gate). Re-flagging them adds noise,
> burns review tokens, and tells the author nothing CI hasn't already
> told them. **Withdraw any candidate finding that matches one of these
> silently** — exactly as for the `do_not_flag` entries above.
>
> Keep only the *judgment residue* a linter can't make: e.g. "this
> `# noqa` lacks a justification", "this is the wrong exception leaf",
> "this `dict[str, Any]` is on a public contract surface". The mechanical
> match itself is CI's job, not the reviewer's.

```yaml
- pattern: "logging hygiene caught by ruff + conformance L-series"
  do_not_flag: true
  added: 2026-06-24
  source: ci-redundancy-audit
  reason: |
    ruff extend-select (G001/G003/G004, T201, LOG009) AND conformance
    L-series both block these. The reviewer must not re-raise them.
  applies_to:
    - "f-string / str-concat / %-format in a logging call (G004/G003/G001)"
    - "print() in production code (T201)"
    - "logger.warn() instead of logger.warning() (LOG009)"
  detection_to_skip:
    - "logging-call argument formatting"
    - "print( in non-test source"

- pattern: "import & exception lint caught by ruff + conformance E-series"
  do_not_flag: true
  added: 2026-06-24
  source: ci-redundancy-audit
  reason: |
    ruff (PLC0415, F401/F403, S110/S112, isort) and the BLOCK-tier
    conformance E-series rules (BareExceptPass / TypedExceptPass) block
    these mechanically. NOTE: MissingExceptionChaining (missing `from
    exc`) is WARN-tier — conformance surfaces it but does NOT block, so
    it stays the reviewer's job (see the WARN-tier note below), not a
    do-not-flag.
  applies_to:
    - "import not at top of module (PLC0415) — unless a justified lazy import"
    - "unused / star imports, import ordering (F401/F403/isort)"
    - "bare or typed except-pass / silent swallow (S110/S112)"
  detection_to_skip:
    - "inline import / except-pass / from-exc presence checks"

- pattern: "single-source BLOCK-tier conformance + Trivy rules"
  do_not_flag: true
  added: 2026-06-24
  source: ci-redundancy-audit
  reason: |
    Each is blocked by exactly one deterministic gate already — a
    BLOCK-tier conformance rule or the Trivy gate. Flag only the
    scoping/judgment residue, never the mechanical match. WARN-tier
    conformance rules are deliberately NOT listed here (CI surfaces but
    does not block them) — see the WARN-tier note below; the reviewer
    still owns those.
  applies_to:
    - "unpinned (non-SHA) GitHub Action ref (conformance C UnpinnedActionReference)"
    - "error-code prefix / category-override mechanics (conformance E/P: ErrorCodePrefixMismatch, CategoryFieldOverride)"
    - "hardcoded secret detectable by Trivy secret scan; dependency CVE-with-fix (Trivy vuln gate)"
  detection_to_skip:
    - "patterns owned by a single blocking conformance/Trivy rule"

- pattern: "coverage percentage threshold (CI coverage gate)"
  do_not_flag: true
  added: 2026-06-24
  source: ci-redundancy-audit
  reason: |
    The sdk-tests job enforces fail_under=85. CI blocks any PR that drops
    below it. Comment only on whether tests are *meaningful*, never the raw %.
  applies_to:
    - "coverage below the configured fail_under threshold"
  detection_to_skip:
    - "raw coverage-percentage comparison"
```

> **Not on this list (still the reviewer's job — no deterministic gate):**
> G3 determinism in `run()`/`@entrypoint`, G2 contract evolution
> (field remove/rename/retype), performance rules, SQL/command-injection
> gating (CodeQL is detect-only / non-blocking), `dict[str, Any]`/`Any`
> on general public signatures (pyright reporters are warnings, not
> errors), and all structural / DX / test-design judgment. Keep
> reviewing those.
>
> **WARN-tier conformance rules — conformance surfaces these but does NOT
> block the PR, so the reviewer is the only enforcement. Flag them; do
> NOT treat them as CI-covered:** missing `from exc` chaining
> (`MissingExceptionChaining`), stdlib `json` over `orjson`
> (`OrjsonOverStdlibJson`), missing integration-test marker
> (`UnmarkedIntegrationTest`), direct `temporalio`/`dapr` import outside
> the adapter seam (`DirectTemporalImport`,
> `PrivateOrchestrationInternalImport`, `TemporalImportOutsideAdapter`,
> `RawTemporalInPublicSurface`), and `logger.critical()` usage
> (`LoggerCriticalUsage`).
