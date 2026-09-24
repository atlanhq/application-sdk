---
kind: responsibility
name: deprecation-area
description: >
  Maintains the current B-series violation-set and drives remediation of
  deprecation findings.  B001 (app: stop consuming a deprecated SDK symbol),
  B007 (app: daft-only DataFrame APIs dead on the daft-less runtime), and
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

The active scope decides which rules can appear: on a consumer app only
B001/B007 (scope `app`) surface; on the SDK only B002/B003/B004 (scope `sdk`).
The runner
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
- `rule_ids` — optional list of exact rule IDs (propagated from the
  top-level entry). Forwarded verbatim into every runner invocation this
  area makes — the loop's detect calls and the suggest-only
  `detect-violations` calls alike — so a `--rule`-scoped run stays scoped
  here rather than silently widening to the whole series at this hop.

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
In the Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "B"
  rule_ids: rule_ids
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "deprecation"`._

Consult the finding's `hint` and `message` — for the B-series the message
carries the SDK's own migration guidance — then read the actual source lines
around `finding.line` in `finding.file` before proposing anything.

**Mechanical fixes** (`classification = "mechanical"`; the loop applies and
gates them, and the edit is fully determined by the finding):

- **B006 StaleContractLedger** (writes `contract_schema.lock.json` in the repo
  root) — a live entrypoint contract field has no entry in the ledger, because
  the ledger was not regenerated after the field was introduced.  The fix is the
  command the finding message already carries, **verbatim, keeping its version
  pin**: `uvx atlan-application-sdk-conformance==<checker version>
  gen-contract-ledger` in a consumer app, or `uv run
  atlan-application-sdk-conformance gen-contract-ledger` inside the SDK repo
  itself.  Never substitute a bare `uv run` in a consumer app: it resolves that
  repo's *locked* conformance dev dependency, and whenever the lock lags the
  release the CI checker runs, the generator rewrites the ledger
  byte-identically and the finding survives — the dead end FND-607 sent a
  developer down on a BLOCK-tier rule.  Set `touched_files` to
  `["contract_schema.lock.json"]` and commit it in the same PR; the write-scope
  carve-out for this file is named in `remediate-finding.prose.md`.
  **Do not delete the ledger first.**  The generator is append-only, which is
  exactly right here — B006 means "this field is absent from the ledger", and
  appending is what records it.  Rebuilding from empty would silently discard
  every genuine removal the ledger records, turning a recorded history into a
  blank one; that is a B005 question and never part of a B006 fix.
  Measured on a connector at suite 0.34.0: two B006 findings, ledger 121 → 123
  fields, re-detect clean with no B005 introduced.

**Guided fixes** (`classification = "judgment"`; the loop applies and gates them
with `recheck-narrowest` + the test orthogonal gate, then routes to residue for
human audit):

- **B005 NonAdditiveContractChange** (app source, the contract class) — a field
  the ledger records is absent from the live contract, or its type changed.
  Work in this order and stop at the first step that applies:
  1. **Suspect a false positive before proposing anything.**  The ledger keys
     entries by **bare class name**, which is not unique, and three independent
     families make a B005 finding noise rather than a real removal: the same
     contract name declared in two modules, where each declaration reads the
     other's fields as removed; a first ledger seeded from the SDK's own
     packaged ledger, which bakes SDK template contracts in permanently because
     the build is append-only; and a class inheriting an SDK *template*
     contract, whose inherited fields the app-side AST scan cannot see.  On one
     connector, all 25 B005 findings were noise of these families.  Check
     whether the named contract is declared more than once in the repo, and
     whether the ledger carries entries for contracts this app never declares
     (SDK template names such as `ExtractionInput` / `QueryExtractionOutput`).
     If either holds this is a **defect in the rule, not in the app**: take the
     `false-positive` path in `remediate-finding` step 5 so
     `report-rule-defect` raises it against the suite.  Never suppress it
     silently.
  2. **Restore the field** when the removal was unintentional and the ledger
     entry gives its type — re-declare it on the contract class with that type.
     Report `classification = "mechanical"` only when the finding names the
     field and the ledger type round-trips; the recheck gate confirms.
  3. **Otherwise route to residue** proposing the owner's choice: restore the
     field, or deprecate and sunset it.
  **Never propose a sunset for a field still referenced anywhere in the repo.**
  Grep the whole tree first, including `scripts/` and `*.sh` JSONPath arguments
  such as `$.extract.outputs.<field>`: a field removed from the contract while a
  DAG node still reads it is a live break, not a tidy-up.  And regenerating the
  ledger is **not** a B005 fix — the generator is append-only and, in the
  finding message's own words, "can never launder a removal".

- **B001 DeprecatedSdkSymbolUsage** (app source) — the app imports, subclasses,
  calls, or reads a symbol the SDK has deprecated.  Apply the migration named in
  the finding message — **never a blind name swap**: the replacement usually
  changes the call shape (signature, return type, import path).  Examples:
  - `DataframeType.daft` → `DataframeType.pandas` — a deprecated **enum member**
    (removal in v4.0.0), marked via the SDK's `__deprecated_members__`
    convention and carried in the generated manifest like any other symbol.
    This one *is* a safe swap: daft already routes to the pandas/pyarrow path.
  - `upload_to_atlan(input)` → `App.upload(UploadInput(local_path=...,
    tier=StorageTier.RETAINED))` — different argument and return types; read the
    call site and adapt both.
  - `from application_sdk.discovery import DiscoveryError` →
    `from application_sdk.errors import InvalidInputError`, and update every
    use of the old name in the file.
  - `class X(BaseMetadataExtractor)` → migrate to `application_sdk.templates.SqlApp`
    per the notice; this is a structural change — draft it and let the test gate
    decide.
  - **SDR test → split by concern**: a finding naming `BaseSDRIntegrationTest`
    (import / subclass from `application_sdk.testing.sdr`) means the app's
    `tests/sdr/test_*_sdr.py` (or `tests/integration/test_sdr.py`) still uses the
    legacy SDR harness. There is no single replacement — SDR is a deployment
    mode, not a test tier — so route `api="auth"` / `api="preflight"` scenarios to
    direct handler calls in the app's own unit or integration tests, leave
    credential resolution to application-sdk's `tests/unit/credentials` (per-app,
    fake secret stores), and move only `api="workflow"` / full-DAG scenarios to
    `tests/e2e/`. The agent-mode e2e below is what satisfies T002 (agent-mode
    credential routing and upload behaviour genuinely differ by mode); it is not
    a wholesale replacement for the suite. Shape it after `atlan-openapi-app` /
    `atlan-metabase-app` `tests/e2e/`:
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
        write each asset through `entity_bytes` to a typed file output passed
        downstream as a `FileReference` (no shared `output_path` scan, no
        `upload_to_atlan()`). Pass the app's declared envelope and the run's
        sync details, not a bare `entity_bytes(asset)`:
        ```python
        from application_sdk.common.asset_serialization import entity_bytes
        from application_sdk.common.entity_envelope import EntityEnvelopePolicy, EnvelopeShape
        from application_sdk.common.last_sync import resolve_last_sync_details

        ENTITY_ENVELOPE = EntityEnvelopePolicy(shape=EnvelopeShape.FLATTENED)

        @task(timeout_seconds=1800)
        async def transform(self, input: TransformInput) -> TransformOutput:
            last_sync = resolve_last_sync_details()  # once per activity
            for record in read_jsonl(input.raw_file, RecordType):
                asset = map_entity(record, connection_qn, workflow_id)
                out_f.write(
                    entity_bytes(
                        asset,
                        connection_name=connection_name,
                        last_sync=last_sync,
                        envelope=ENTITY_ENVELOPE,
                    )
                    + b"\n"
                )
            return TransformOutput(output_file=FileReference(local_path=str(output_file)))
        ```
        **Choose the envelope from the connector's released output, never by
        default.** `FLATTENED` puts relationship refs in `attributes`; a connector
        whose released output already has them under a top-level
        `relationshipAttributes` key (the shape `to_nested_bytes()` wrote) must
        pin `EnvelopeShape.PYATLAN` for this migration, so neither its wire
        format nor its publish diff cache flips as a side effect.
        `PYATLAN` is a deprecated one-cycle lever (removed in v4.0): moving to
        `FLATTENED` is a separate, deliberate change. Drop `connection_name` /
        `last_sync` only when the mapper already stamps both on every asset.
      - Drop the YAML query templates, the `TransformerInterface` subclass, and any
        Daft DataFrame use that existed only to feed the transformer. Full guidance:
        `docs/upgrade-guide-v3.md` (Step 2 / asset-mapper section).
  Because the migration is non-trivial, `classification` is always `"judgment"`.
  The orthogonal test gate is what makes applying it safe: if the migration
  breaks behaviour, the gate reverts and routes to residue.

- **B007 DaftOnlyDataframeApiUsage** (app source) — a daft-only DataFrame API is
  used on frames the SDK hands the app; the daft-less SDK runtime returns
  **pandas**, so the call raises `AttributeError` at runtime while imports and
  mocked tests stay green (latent-on-main breakage found in the fleet SDR
  sweep).  Apply the pandas migration named in the finding message:
  - `frame.count_rows()` → `len(frame)`;
  - `frame.to_pylist()` → `frame.to_dict("records")` (pyarrow-Table receivers
    are already exempted by the checker — `pa.Table.to_pylist()` is real);
  - `frame.names` → `frame.columns`.

  `DataframeType.daft` is **not** a B007 finding — it is an SDK symbol and
  arrives as **B001** from the generated deprecation manifest.  The migration
  is the same (`DataframeType.pandas`), but read it off the B001 finding, whose
  message carries the SDK's own notice text.

  Do NOT fix these one CI cycle at a time: run the app's transforms locally
  against synthetic raw data and migrate every call in one pass.  Matching is
  attribute-name-anchored, so when the receiver is genuinely not an SDK reader
  frame propose a `# conformance: ignore[B007] <reason>` suppression instead.
  `classification` is always `"judgment"`.

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
