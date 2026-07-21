---
kind: responsibility
name: deprecation-area
description: >
  Maintains the current B-series violation-set and drives remediation of
  deprecation findings.  B001 (app: stop consuming a deprecated SDK symbol) and
  B002 (sdk: fix a malformed deprecation notice) are guided fixes; B003 (overdue
  removal) and B004 (unmarked claim) are detect-only and route to residue.
---

### Maintains

The current set of unsuppressed B-series (backwards-compatibility / deprecation)
conformance findings in the working tree, classified by disposition and
remediability.

#### violations-deprecation

The fingerprint-set of all unsuppressed FAILING B-series results in the current
working tree, as reported by `suite.runner --series B`.

B-series rules are WARN-tier, so in **default** mode this facet is typically
empty (warnings do not fail the gate).  In **strict** mode the fingerprint-set
includes unsuppressed WARNING results, which is where B-series remediation
actually runs.

The active scope decides which rules can appear: on a consumer app only B001
(scope `app`) surfaces; on the SDK only B002/B003/B004 (scope `sdk`).  The runner
auto-detects scope, so each repo only ever sees its own half.

This facet's fingerprint moves when any B-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series B` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for B-series in the SARIF output is 0 (every
> B-series WARNING was cleared by a real fix or a justified suppression).

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
In the Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "B"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "deprecation"`._

Consult the finding's `hint` and `message` — for the B-series the message
carries the SDK's own migration guidance — then read the actual source lines
around `finding.line` in `finding.file` before proposing anything.

**Guided fixes** (`classification = "judgment"`; the loop applies and gates them
with `recheck-narrowest` + the test orthogonal gate, then routes to residue for
human audit):

- **B001 DeprecatedSdkSymbolUsage** (app source) — the app imports, subclasses,
  or calls a symbol the SDK has deprecated.  Apply the migration named in the
  finding message — **never a blind name swap**: the replacement usually changes
  the call shape (signature, return type, import path).  Examples:
  - `upload_to_atlan(input)` → `App.upload(UploadInput(local_path=...,
    tier=StorageTier.RETAINED))` — different argument and return types; read the
    call site and adapt both.
  - `from application_sdk.discovery import DiscoveryError` →
    `from application_sdk.errors import InvalidInputError`, and update every
    use of the old name in the file.
  - `class X(BaseMetadataExtractor)` → migrate to `application_sdk.templates.SqlApp`
    per the notice; this is a structural change — draft it and let the test gate
    decide.
  - **SDR test → agent-mode e2e**: a finding naming `BaseSDRIntegrationTest`
    (import / subclass from `application_sdk.testing.sdr`) means the app's
    `tests/sdr/test_*_sdr.py` (or `tests/integration/test_sdr.py`) still uses the
    legacy SDR harness. Migrate to the agnostic e2e harness — a `BaseE2ETest`
    subclass, normally via the generated `*GeneratedE2EBase`, with a class-level
    `mode = RunMode.AGENT` (agent mode *is* the self-deployed-runtime path). Shape
    it after `atlan-openapi-app` / `atlan-metabase-app` `tests/e2e/`:
    ```python
    from application_sdk.testing.e2e import RunMode
    from app.generated._e2e_base import MyAppGeneratedE2EBase

    @pytest.mark.e2e
    class TestMyAppE2E(MyAppGeneratedE2EBase):
        mode = RunMode.AGENT
    ```
    Guard the import so the file is a clean skip on older SDKs
    (`try: ...; except ImportError: pytest.skip(allow_module_level=True)`), carry
    over any `manifest_path`-derived assertions the SDR suite validated, then delete
    the `test_sdr.py`. Structural change (always `"judgment"`) — draft it and let the
    test gate decide.
  - **Legacy transformer → asset-mapper** (BLDX-1399): a finding naming
    `TransformerInterface`, `AtlasTransformer`, or `QueryBasedTransformer`
    (import / subclass / call of `transform_metadata` / `transform_row`) means the
    app still runs the half-YAML/half-code transformer path (YAML query templates +
    DuckDB + memory-heavy DataFrames). Migrate it to the v3-native **asset-mapper**
    pattern — pure Python functions that map typed records directly to `pyatlan_v9`
    Asset instances, eliminating the Daft/YAML dependency. This is a structural
    rewrite (always `"judgment"`); draft it and let the test gate decide. Shape it
    after the reference apps `atlan-openapi-app` and the migrated
    `atlan-metabase-app`:
      - **File layout** — `app/api_types.py` holds the typed intermediate records
        (`@dataclass` or `msgspec.Struct`); `app/asset_mapper.py` holds pure
        `map_<entity>(record, connection_qn, ...) -> <pyatlan_v9 Asset>` functions
        (no I/O, deterministic); the `transform` task lives in the connector/app
        module.
      - **Mapper function** — construct the asset from the typed record, set
        attributes, stamp sync metadata, and `return` the asset:
        ```python
        from pyatlan_v9.model.assets import Table

        def map_table(record: TableRecord, connection_qn: str, workflow_id: str) -> Table:
            asset = Table(
                qualified_name=f"{connection_qn}/{record.database}/{record.schema}/{record.name}",
                name=record.name,
                connector_name="my-connector",
                connection_qualified_name=connection_qn,
            )
            asset.status = "ACTIVE"
            asset.last_sync_run = workflow_id
            return asset
        ```
      - **Transform task** — read typed records from the input JSONL, map each, and
        write `asset.to_nested_bytes()` to a typed file output passed downstream as a
        `FileReference` (no shared `output_path` scan, no `upload_to_atlan()`):
        ```python
        @task(timeout_seconds=1800)
        async def transform(self, input: TransformInput) -> TransformOutput:
            for record in read_jsonl(input.raw_file, RecordType):
                asset = map_entity(record, connection_qn, workflow_id)
                out_f.write(asset.to_nested_bytes() + b"\n")
            return TransformOutput(output_file=FileReference(local_path=str(output_file)))
        ```
      - Drop the YAML query templates, the `TransformerInterface` subclass, and any
        Daft DataFrame use that existed only to feed the transformer. Full guidance:
        `docs/upgrade-guide-v3.md` (Step 2 / asset-mapper section).
  Because the migration is non-trivial, `classification` is always `"judgment"`.
  The orthogonal test gate is what makes applying it safe: if the migration
  breaks behaviour, the gate reverts and routes to residue.

- **B002 MalformedDeprecationNotice** (SDK source) — the notice is missing a
  migration target and/or a removal version.  Edit the notice string in place to
  add what the finding says is missing:
  - missing migration target → add `use <replacement>` naming the real successor
    (read the surrounding code / docstring to find it);
  - missing removal version → add `will be removed in v<N>`, choosing the next
    major unless the surrounding context names a version, and **state that
    assumption** in the edit.
  `classification` is `"judgment"` (the wording and target need a human-level
  call); the recheck gate confirms the notice now parses as well-formed.

**Detect-only — route to residue** (`not_remediable = true`):

- **B003 OverdueDeprecationRemoval** (SDK source) — the symbol was promised gone
  by a version the SDK has already reached.  Resolving it means *removing a public
  symbol* or *pushing out the removal version* — both are human decisions with
  fleet-wide blast radius, so never auto-edit.  Record in residue with the
  finding message (which names the overdue version and current version).

- **B004 UnmarkedDeprecationClaim** (SDK source) — a docstring claims deprecation
  with no marker.  Which marker to add (`@deprecated` decorator vs a
  `DeprecationWarning` in `__init__`/`__init_subclass__`) is a small design
  choice for the symbol's owner; record in residue with the suggestion the
  finding message already carries.

**Suppress outcome (strict mode only, WARNING-tier findings)**: the model may
propose an inline `# conformance: ignore[Bxxx] <8–40 word justification>` when
the site is a legitimate exception (e.g. a B001 usage in a compatibility shim
that intentionally bridges old and new APIs).  Route every suppression to residue
for human audit.
