# ADR-0020: Artifact Validation — One Wrapper, Per-Format Validators, App-Owned Declarations

## Status
**Accepted**

## Context

Data crosses app boundaries as files. A connector writes NDJSON entities that the publish app reads;
a miner writes partitioned parquet that a parser reads; a parser writes parquet-with-a-JSON-blob that
a lineage step reads. At every one of those hand-offs, the producer's idea of the artifact's shape and
the consumer's idea of it are independent beliefs, and nothing checks that they agree.

The cost of that is measured. A production RCA traced a frozen lineage marker — 73 days, 12
connections — to a single column that had become a string where the consumer expected a timestamp.
Every workflow in the chain reported success for all 73 days, because every workflow was individually
doing exactly what it was told.

The SDK already guarantees the layer beneath this. `storage/integrity.py` attests that the bytes a
consumer reads are the bytes the producer wrote, and it is explicit about what that does *not* prove:

> The sidecar attests to the bytes the SDK read at upload time, not to the artifact being
> semantically complete. A producer that wrote a truncated file to disk and *then* handed it to
> `upload_file` gets a sidecar recording the truncated content as the expected digest, and every
> downstream check passes.

Byte integrity plus atomic writes plus a semantic check is the full set. This ADR adds the third leg.

Three constraints shaped the design more than anything else:

1. **The declaration is not ours.** Which fields exist, which types they carry, which are required —
   all of that belongs to the app that consumes the artifact. An earlier design attempt (FND-397) set
   out to define a canonical query-history schema and was cancelled once review established that a
   schema neither the producer nor the consumer owns is a schema nobody maintains.
2. **The cost of a check must match the question.** "Is `START_TIME` a timestamp?" is answered by one
   parquet footer read. Answering it by loading rows into a dataframe pays a dataframe to do a
   metadata lookup, and drags pandas into the runtime path of callers that only ever see JSON.
3. **A check nobody believes is worse than no check.** The asset validator shipped at ~98% false
   positives from a single decoder mismatch (FND-113) and cost real trust to recover.

## Decision

A thin, format-agnostic **wrapper** that takes an app-owned declaration, dispatches on format, and
owns only the shared outcome: one report shape, one outcome event, one bounded drill-down payload.

The wrapper has **two orthogonal plug-in seams**. Keeping them separate is the load-bearing choice —
conflating them is what forces either a dataframe dependency or a hand-authored field list.

| Seam | Question | Plug-ins |
| --- | --- | --- |
| **Schema source** | where does the declaration come from? | `ModelSource` (an executable typed model) · `ContractSource` (the app's generated contract artifact) |
| **Format validator** | how is it checked for this format? | NDJSON (streaming) · parquet (footer) |

### Schema sources

**`ModelSource`** — an executable typed model, e.g. pyatlan_v9's `Asset`. Nothing is authored: the
model *is* the declaration. This is what makes the deeply-nested case tractable — 500+ types and
4000+ properties with diamond inheritance, which no one could reasonably re-declare by hand. The
check delegates to the model's own `.validate()`, which `application_sdk/validation/assets.py`
already does.

**`ContractSource`** — the app's generated `app/generated/artifact_schemas.json`, declared in the
app's own `contract/app.pkl` and emitted by the contract toolkit. Loaded with `orjson` (a core
dependency).

**There is no inline source.** No literal field map, no dict escape hatch, not even for a three-field
artifact. Every declaration is version-controlled. This is deliberate: the contracts FND-397 found in
the field were prose comments spread across each app's source, load-bearing and well written, which
is exactly the trap — a comment can faithfully document a workaround, so drift gets recorded *as the
spec*. A "just this once" inline map is how that state is reached, and authoring a real declaration is
cheap enough that convenience is not a justification for a second, unversioned declaration path.

Two consequences follow, and both are improvements:

* Tests use a committed generated-artifact fixture rather than a hand-built object, so the fixture
  exercises the real loader path.
* Apps whose storage facade bypasses the framework still get the public `validate_artifact(...)`
  entry point — but they pass a `ContractSource`. The escape hatch is about *where the call happens*,
  never about *where the declaration lives*.

### Declaration is mandatory at the public boundary, optional internally

The only permitted bypass is *no declaration at all*, and only for app-internal data.

| Surface | Declaration | Rationale |
| --- | --- | --- |
| Every entrypoint's `input_type` / `output_type` | **Required** for every `FileReference` field | A public, external-facing interface by definition — another app or the DAG reads it |
| Internal `@task` contracts | **Optional** | Purely app-internal processing; the app decides whether it wants the check or accepts the risk |

The boundary needs no special-casing, which is what makes it cheap to enforce: the default `run()`
method is registered as an *implicit* entrypoint carrying the same `EntryPointMetadata` as an
explicit `@entrypoint`. "The public boundary" is therefore uniformly every `EntryPointMetadata`'s
`input_type` and `output_type` — one rule, explicit and implicit alike.

Three enforcement layers, because each catches what the others cannot:

1. **Registration time** — a missing declaration on a boundary `FileReference` is the same class of
   defect as the contract-shape violations that already raise `EntryPointContractError`. Because v3
   has shipped consumers, this warns in 3.x and raises in 4.0, with the removal version named in the
   warning. Note this needs only a deprecation window, not the false-positive gate the *content*
   checks need: "no declaration exists" is a structural fact, so it cannot be a false positive.
2. **Static, pre-merge** — conformance rule K016.
3. **Runtime** — every undeclared ref emits `outcome="not_declared"` with a `boundary` attribute: a
   finding on the boundary, informational internally. Both emit; neither is silent.

### Format validators, each with its own dependency floor

| Format | Check | Cost | Dependency floor |
| --- | --- | --- | --- |
| **NDJSON** | stream line by line; assert declared field presence and type per record | one pass, constant memory, no dataframe | stdlib / `orjson`, already core — **zero new deps** |
| **parquet** | read the schema from the file footer, diff against the declaration | **metadata only — no rows read** | `pyarrow.parquet.read_schema`; pyarrow is an extra, so lazy import and degrade to skip-with-warning when absent |

The plug-in unit is a *check strategy*, not a bare schema comparison, because a model source implies
delegation while a field-map source implies diffing:

| | `ModelSource` | `ContractSource` |
| --- | --- | --- |
| **NDJSON** | per-record decode + `.validate()` | per-record declared-key presence + JSON type check |
| **parquet** | **unsupported** — a model carries no column mapping. Emits `outcome="unsupported"`, never silence | footer schema diff: names + logical types |

### Declaration home: a contract-toolkit block

App-owned declarations live in the app's `contract/app.pkl` and generate
`app/generated/artifact_schemas.json`.

The toolkit is **transport, not owner**. Every field is authored by the app; the SDK ships no field
list; the artifact is per-app. What FND-397 rejected was one canonical schema that neither producer
nor consumer owned — this is its opposite. Persisting the declaration buys three properties an
in-code declaration cannot have: it is versioned and pinned, it is readable by non-Python consumers,
and it is **statically diffable** by a conformance rule.

For a cross-app hand-off, the **consumer** declares what it requires of its input, and a producer
**references the consumer's published artifact by pinned version** rather than re-authoring the field
list. Ownership stays with the consumer.

Declarations are keyed by `(entrypoint, output-contract field name)`. The interceptor knows both, so
nothing is inferred from path shape — path-shape inference is precisely what made the earlier
upload-time hook silently validate nothing.

#### The logical-type vocabulary

The SDK owns the type vocabulary; every field that uses it is the app's. Two aliases, the second
layering additively on the first:

```pkl
/// Types every validator must map, for every format. The stable floor.
typealias ArtifactFieldType =
  "string"|"int"|"float"|"bool"|"timestamp"|"date"|"json"|"any"

/// Additive extension. A member here may resolve to "unsupported for this
/// format" without widening the floor above.
typealias ArtifactFieldTypeExtended =
  ArtifactFieldType|"decimal"|"binary"|"time"|"array"|"struct"|"map"
```

Field declarations use `ArtifactFieldTypeExtended`, so the base stays a guaranteed floor while
extension members can be declared before every validator supports them.

Each member earns its place against a specific failure rather than mirroring arrow wholesale:

| Type | Why it exists |
| --- | --- |
| `string`, `timestamp` | the 73-day RCA — a stringified timestamp column. The one distinction the capability exists to make |
| `int` | `YEAR`/`MONTH`/`DAY` partition columns, counts, epoch-millis fields |
| `float` | durations and credit/byte measures in query history |
| `bool` | flags such as `isManagedAccess` |
| `json` | a hop whose parquet carries a JSON blob in one column: physically a string, semantically not. Collapsing the two defeats that hop's check |
| `date` | `date32` partition columns |
| `any` | "must be present, type not asserted" — lets a thin contract declare presence without anyone inventing a wrong type to satisfy the enum |
| `decimal` | Snowflake `NUMBER` lands as parquet `decimal128`; asserting `float` would mis-flag or lose precision |
| `binary`, `time`, `array`, `struct`, `map` | real in warehouse sources but on no current hop — extension members |

Nested payloads use **dotted field paths plus a container type**, not a recursive type grammar. The
deeply-nested asset case never needs the grammar because it delegates to its model.

Per-format mapping (reviewed once here rather than becoming per-validator folklore):

| Logical | parquet / arrow | NDJSON / JSON |
| --- | --- | --- |
| `string` | `string`, `large_string` | JSON string |
| `int` | any `int*` / `uint*` | JSON number, integral |
| `float` | `float*`, `double` | JSON number |
| `decimal` | `decimal128`, `decimal256` | JSON number **or** string (both are lossless carriers) |
| `bool` | `bool` | JSON `true`/`false` |
| `timestamp` | `timestamp[*]` — **any unit, tz-aware or not** | ISO-8601 string or epoch number |
| `date` | `date32`, `date64` | ISO-8601 date string |
| `time` | `time32`, `time64` | ISO-8601 time string |
| `binary` | `binary`, `large_binary`, `fixed_size_binary` | base64 string |
| `json` | `string` whose content parses as JSON | nested object/array, or a JSON-in-string |
| `array` | `list`, `large_list`, `fixed_size_list` | JSON array |
| `struct` | `struct` | JSON object |
| `map` | `map` | JSON object |
| `any` | any type | any type |

The load-bearing row is `timestamp`: arrow `timestamp[*]` satisfies it at any unit, tz-aware or not,
and arrow `string` does **not**. That single asymmetry is the check that would have caught the RCA.

### Enforcement points

`FileReference` is the universal hand-off token, and the activity interceptor is the one place every
`@task` in every app funnels through. Both enforcement points come off one declaration at one site:

```
  materialize_file_refs(...)     # durable -> local
+ validate(ingest)               <- consumer side: re-validate on read
  result = await task_method(input_data)
+ validate(handoff)              <- producer side, BEFORE persist: bytes are
  persist_file_refs(...)            still local, so blame lands on the producer
```

The declaration is discovered from contract-field metadata exactly as the `Lazy()` marker is today,
inside the same tree walk. No new traversal.

**No silent no-op.** The earlier upload-time hook returns early and emits *nothing* when its path gate
does not match, so an app can look adopted while validating zero records. Here every artifact emits an
outcome, including the negative ones — `not_declared`, `unsupported`, `absent`. A check that reports
nothing is indistinguishable from a check that passed, and that ambiguity is itself a defect.

| Point | Check | Day 1 | Graduation |
| --- | --- | --- | --- |
| Producer, before hand-off | validate emitted records against the consumer's declaration | warn + outcome event | per-app opt-in to block, failing the producing activity. Best blame, blast radius one workflow |
| Consumer, at ingest | re-validate on read | warn + outcome event | stays on permanently — the only cover for producers that are not our code, and for artifacts already written |
| Static, pre-merge | K015, K016 | `warn` | `block` once clean |
| Boundary declaration missing | registration-time check | warn, removal version named | raise in 4.0 — a deprecation window, not an FP gate |

### Posture: the app declares whether a verdict blocks

Mirroring the preflight gate: `App.artifact_validation_mode` plus an env override, resolved **once at
worker build** and baked into the closure. Only the literal `"hard"` enforces, so blocking is always
a deliberate opt-in. A single emit site reports `"blocked"` or `"would_block"`, and a second axis
separates "the artifact is unverifiable" (subject to mode) from "our validator broke" (always fails
open). A posture event fires at worker build for every app, soft included — that is the denominator
the outcome events cannot supply.

**Nothing graduates to blocking until its false-positive rate is measured from the outcome events.**

### One report, one event, and a registry for event names

The report mirrors `AssetValidationReport`: scalar counts, a bounded failure list, and a
`format_report(*, max_items=...)` renderer. Bounding is two-tier and unchanged — the scan is
unbounded (every record is examined) and only the two *output* surfaces are capped, by one shared
constant so the human report and the telemetry matrix can never drift. The matrix attribute is always
present, even empty, so consumers never branch on its presence.

Emitting a queryable event is a three-part contract, or the fields silently never reach OTLP: a pinned
event-name constant (the name is the log message body), an attribute-key constant, and entries in the
known-extra-keys allowlist.

This is the **fourth** pinned-name outcome event, after the two preflight-gate events and the
asset-validation one. The existing note that a shared event-name registry is "worth adding only once a
third such event exists" is satisfied, so the registry lands with this work and the existing names
move into it unchanged.

### Absorbing the existing asset validator

`validate_transformed_dir` becomes the NDJSON × `ModelSource` check inside the wrapper, and the
upload-time hook is re-pointed at it. **The existing event name and attribute keys are preserved
verbatim** — dashboards, the drill-down matrix attribute and alert rules key off those exact strings,
and v3 has shipped consumers. This is a refactor behind a stable event surface, not a rename.

## Options Considered

### Option 1: Two seams, app-owned persisted declarations (Chosen)

**Pros:**
* The expensive mechanism is never on the cheap path — a JSON-only caller never loads pyarrow, and a
  parquet type check never loads a row.
* The intractable case costs nothing to declare, because an executable model is a valid source.
* Declarations are versioned, pinned, statically diffable, and readable by non-Python consumers.
* One report, one event, one registry entry across every format.
* Both enforcement points come off one declaration at one site.

**Cons:**
* Two seams is more structure than a single entry point.
* A toolkit change carries downstream-compatibility obligations for every consumer repo.
* The parquet × model cell is genuinely unsupported and must say so rather than guess.

### Option 2: One pandera/pandas entry point

Promote the existing test-time `validate_with_pandera` to the runtime path.

**Pros:** it exists; value-range and statistical checks come free.

**Cons:** drags pandas into the runtime path of callers that only ever see JSON — wrong memory
profile, wrong CVE footprint. It `json_normalize`s everything into one in-memory frame, the opposite
of the streaming posture the asset validator was built to hold. And it answers a metadata question by
reading rows. **Rejected**; pandera stays test-only, where value ranges, record counts and statistical
checks are exactly right.

### Option 3: A canonical schema owned by the SDK

One shared query-history record all miners converge on, with an SDK-side column vocabulary.

**Cons:** a schema neither the producer nor the consumer owns is a schema nobody maintains, and it
puts a column list in shared SDK code, where it becomes a per-app allowlist by another name.
**Rejected in FND-397 review**, which is why this ADR is about mechanism only.

### Option 4: Declarations in app Python, no persisted artifact

**Pros:** no toolkit change; no generated artifact.

**Cons:** not statically diffable, so the "declaration disagrees with the writer" class of drift can
only be caught at runtime, after the artifact is written; not readable by non-Python consumers; and
in practice it degrades toward inline literals, which is how prose-comment contracts happen.
**Rejected.**

### Option 5: Hook the upload path instead of the interceptor

**Cons:** inherits both known holes of the existing hook — apps with their own storage facade never
reach it, and its path gate returns early without emitting, so an app can report as adopted while
validating nothing. **Rejected**; the interceptor is the convergence point every task passes through.

## Consequences

* Every artifact hand-off can be checked against a declaration the consuming app owns, at a cost
  proportional to the question asked.
* Declaration is mandatory at the public boundary. Internal tasks may opt out — explicitly, and
  visibly in the outcome events, not by omission.
* Adding a format means adding one validator with its own dependency floor. Nothing existing moves.
* Contracts thicken over time and the mechanism gets stronger for free: the wrapper does not care
  whether a declaration names three fields or thirty.
* A toolkit-block change means every consumer repo regenerates. The new artifact must be optional so
  that an app declaring nothing generates byte-identical output.
* pandera and pandas remain test-only. This is a standing constraint, not a temporary state.

## References

* `application_sdk/validation/assets.py` — the NDJSON asset validator this absorbs.
* `application_sdk/storage/integrity.py` — byte-level attestation, and its explicit statement of what
  it does not prove.
* `application_sdk/execution/_temporal/activities.py` — the interceptor seam.
* `application_sdk/execution/_temporal/preflight_gate.py` — the soft/hard posture pattern, the
  single-emit-site rule, and the classification axis.
* [ADR-0006](0006-schema-driven-contracts.md) — single-model contracts; declarations attach to those
  contract fields.
* [ADR-0013](0013-error-hierarchy-and-failure-taxonomy.md) — failure taxonomy the classification axis
  routes off.
* [ADR-0017](0017-native-execution-isolation.md) — why the model path runs isolated.
* Conformance K006 — the cross-artifact static diff K015 and K016 are modelled on.
