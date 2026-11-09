---
name: migrate-asset-modeling
description: >
  Move a connector app's asset modeling onto the canonical shape: asset
  identities (qualifiedNames) from pyatlan_v9 creators (P028), entities built
  as pyatlan_v9 assets (P058, O003, O004, P055) and written through the SDK's
  entity_bytes seam (P052, O002). Two things must not change: an asset's
  identity, and what the app publishes, except the stamps entity_bytes adds by
  design. The skill sorts every site into a group, applies the group's end
  state, and proves both invariants with re-detection, the test suite and an
  output diff. A site whose fix needs something pyatlan does not have yet stays
  open and says what pyatlan must add. Run after migrate-off-daft when the app
  is below SDK 3.20.0; adopting creators needs SDK 3.39.0 or later.
runs_before: [migrate-deprecated-symbols]
mandatory_triggers:
  - "/migrate-asset-modeling"
  - "migrate asset modeling"
  - "P028 qualifiedName f-string"
  - "entity_bytes migration"
optional_triggers:
  - "use pyatlan creators"
  - "pyatlan_v9 migration"
  - "to_nested_bytes replacement"
owner: connector-platform-team
last_updated: "2026-11-09"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - qualifiedNames from pyatlan_v9 creators; a pyatlan gap left as one unsuppressed helper per name shape
  - entities built as pyatlan_v9 assets and written with entity_bytes(asset, envelope=...)
  - pyproject.toml and uv.lock (SDK raised to >= 3.39.0 only when creator-built assets are emitted)
  - tests that pin the app's identities and output before and after
---

# Migrate asset modeling (P028, P052, P055, P058, O002, O003, O004)

## What this protects

- **Identity.** A qualifiedName is the asset. A changed string is a new asset:
  duplicates on the next run, orphans for everything linked to the old one.
  The grammar belongs to pyatlan's `creator()` factories, so one fix there
  reaches every app. A P028 finding the app cannot close yet is how the fleet
  finds a missing creator: never hide it.
- **What the app publishes.** `entity_bytes` is the one seam where the SDK
  stamps `connectionName`, applies the declared envelope, sets top-level
  `status: ACTIVE` and strips the placeholder guid every creator sets (SDK
  3.39.0). Those changes are expected. Any other change to the output is a
  defect unless an owner accepts it.

So every group below asks the same two questions: does the identity stay the
same, and does the output stay the same?

The rule definitions own what "correct" is: each finding carries its rule's
`full_description` and `terminal_state`. This skill owns how to tell the sites
apart, what evidence each needs, and how to prove the change.

## Judge inputs by where they come from

A creator check or a parity check is only as good as its inputs. An input
matters when a real source can send it.

1. Name where every input you test comes from. Never make up an input to see
   whether a creator breaks.
2. Follow the value back to its producer: a guard, a validated type, or the
   code that builds it. Example: a connection qualifiedName comes from the
   Atlan connection, and `Connection.creator` always builds
   `default/<connector>/<epoch>`, so a one-segment connection qualifiedName
   cannot reach the site.
3. Where the value comes straight from the source system with no check (an
   API field, a catalog column), treat any non-blank string as possible,
   unless a fixture recorded from that source shows its format. Say which
   fixture.
4. Blank or `None` is possible wherever `.get(k)`, `.get(k, "")` or a
   `str = ""` field lets it through.

## Groups

Each group: **when** (what you see at the site), **why** (what is at stake),
**end state**. The group id is the `fix_shape` the decided defaults use.

### P028 — identity from creators

Check with the installed pyatlan, never from memory:
`"creator" in vars(X)` (Process, ColumnProcess, BIProcess:
`"generate_qualified_name" in vars(X)`). An inherited creator is not this
type's creator. Then build the string both ways with realistic inputs and
compare; for a chain of creators, check every id in the chain.

- `creator-string` — when a creator gives the same string for every input the
  source can send. End state: a memoized helper that returns
  `X.creator(...).qualified_name`; when only the id is known, pass it as
  `name` too and never emit that asset.
- `creator-blank-raises` — when the creator is the same except that it raises
  on a blank or `None` input the app accepts. Why: the f-string would publish
  `…/None` or `…/`, a broken identity. End state: as `creator-string`, no
  guard and no fallback; the PR lists each input that now raises.
- `process-id` — when the site builds a Process, ColumnProcess or BIProcess
  identity `{connection_qn}/{suffix}`. End state:
  `generate_qualified_name(..., process_id=<suffix>)`, identical by
  construction. Never pass `process_id=None`: that falls back to a hash.
- `not-identity-ignore` — when the string never reaches a `qualifiedName`
  field or a relationship reference (object-store key, prefix match, lookup
  key, log or display text). Prove it by following every reader. End state:
  `# conformance: ignore[P028] <kind>, not a qualifiedName` on the line
  above.
- `decode` — code that splits a qualifiedName apart. Not a P028 site; leave
  it.
- `gap` — when no creator can produce the string for an input the source can
  send. Name which, in the brief: `no-creator` (the type has none),
  `differs` (the creator gives another string), `missing-input` (the creator
  needs a value the source record does not carry; name it), or
  `rejects-input` (the creator raises on a non-blank value the source can
  send; name the value and where it comes from). End state: the f-string in
  one helper per name shape, unsuppressed, until pyatlan adds the creator.

### P052 — one serialization seam

The envelope must keep what the app publishes today. Decide it from the
app's released output, not from the method name.

- `reshaped-flattened` — when the app decodes `to_nested_bytes()` and moves
  `relationshipAttributes` into `attributes` (or drops them). That rebuilds
  `EnvelopeShape.FLATTENED` by hand. End state:
  `entity_bytes(asset, envelope=FLATTENED)`; delete only the hand
  flattening. App-specific changes on top (a renamed field, a stripped
  reference key, extra attributes) stay as a step on the decoded
  `entity_bytes` output, unchanged, so the output stays the same.
- `nested-released` — when the released output still has a top-level
  `relationshipAttributes` key. End state: `EnvelopeShape.PYATLAN`
  (deprecated, removed in v4.0). Moving it to `FLATTENED` changes the wire
  shape: owner decision.
- `to_atlas_format` call sites are `reshaped-flattened` with nothing to
  delete.

### P058 — entities as pyatlan_v9 assets

Why: a hand-built dict has no model checking its keys and no creator owning
its identity, and outside `entity_bytes` it misses `status: ACTIVE`, so a
soft-deleted entity that comes back stays deleted.

- `asset-creator` — when the dict is emitted for publish and its type has a
  creator. End state: the creator plus model fields, through `entity_bytes`.
  A key the model cannot hold goes back on the decoded `entity_bytes` output.
  The qualifiedName rules of P028 apply to the creator call.
- `asset-no-creator` — when the type is modeled but has no creator of its own
  (for example `Function`). End state: the model class built directly with
  the same keys, through `entity_bytes`.
- `process-asset` — when the dict is a Process, ColumnProcess or BIProcess.
  End state: `Process.creator(..., process_id=<suffix>)` with typed
  input/output references; same unit as the P028 `process-id` sites of the
  same identity.
- `api-patch` — when the dict is sent straight to the Atlan API as a partial
  update (for example a POST to `/api/meta/entity/bulk`), not written for
  publish. Why: the publish stamps would overwrite the asset's real values.
  Hold for the owner.
- `nondata-lane` — when the dict is staged for publish's non-data lane
  (glossaries, domains), which keys entities by its own path. Hold for the
  owner.
- `unmodeled-ignore` — when `pyatlan_v9` has no class for the type. End
  state: the dict keeps top-level `"status": "ACTIVE"` on every non-delete
  upsert and carries `# conformance: ignore[P058] <type> is not modeled in
  pyatlan_v9`.

An app whose mappers all emit dicts moves as one unit, not site by site.

### O004 — pyatlan_v9 models

- `import-only` — when every construction keyword exists on the v9 class and
  no legacy method (`to_atlas_dict()`, `.dict()`, `attributes=`) is used. End
  state: the import from `pyatlan_v9.model.assets`.
- `with-serialization` — when the models feed a mapper that also has P052 or
  P058 sites. End state: move it in the same unit as those sites; the import
  alone breaks the mapper.
- `search-client` — when the models feed a search or lookup through the
  legacy client (`FluentSearch`, `asset_type=`). Moving it means the
  pyatlan_v9 client too. Hold for the owner.
- `legacy-pin` — when the app keeps the legacy `AtlasTransformer` on
  purpose. End state: `# conformance: ignore[O004] <the pin>`.

### O002, O003, P055

- O002: a `.dict()` on an asset goes through `entity_bytes`; a `.dict()` on a
  non-asset model is a false positive, ignored with that reason.
- O003: add the return annotation (`-> Table`, a union, `Optional`).
- P055: set the single reference on each child
  (`column.table = RelatedTable(...)`), derived from the child's own source
  record, and leave the parent's list unset. A child emitted only through the
  parent's list is an owner decision: the app must start emitting it.

### Order inside one mapper

O004 → O003 → P028 → P052 / O002 → P055 → P058. Sites that share a mapper
or an identity move in one unit.

## Before you change anything

1. SDK version from `uv.lock`: 3.39.0 or later, or raise it (newest release
   at least 7 days old) when the app will emit creator-built assets. Below
   3.20.0: stop and run `migrate-off-daft`.
2. A creator missing in the locked pyatlan but present in a release at least
   7 days old inside the allowed range: with a developer, raise pyatlan in
   the lock; in the remediation lane the site is held (decided defaults) for
   that raise. No such release: the site is `gap`.
3. Baseline:
   `atlan-application-sdk-conformance detect --rule P028,P052,P055,P058,O002,O003,O004 --exit-zero --output before.sarif`
   and the test suite; record tests that already fail.
4. Capture today's output from the offline (recorded) integration test,
   one JSONL line per entity, outside the repo. No such test: record that
   delivered-output parity is not proven, and pin each touched type's
   qualifiedName in a unit test before any edit.

## Prove it

1. Re-detect with the baseline command: only `gap`, owner-held and ignored
   sites remain.
2. Tests: no failure that was not in the baseline.
3. Identity: the `(typeName, qualifiedName)` pairs and every
   `uniqueAttributes.qualifiedName` in references, compared as multisets
   (counts), are equal before and after. P055 moves references by design:
   compare `(parent, child)` pairs instead.
4. Output (P052, P058, O004): every entity's attributes equal before and
   after, except `connectionName` added, `status: ACTIVE` added, placeholder
   guids removed and volatile `lastSync*` values.
5. Pin it: commit the before-snapshot next to the recorded fixture and
   assert `Counter` equality in the offline test. Break one grammar on
   purpose, see the test fail, revert.

## PR description

- per group: the sites and the end state applied;
- every input that now raises (`creator-blank-raises`, `process-id`);
- every output difference and why it is expected;
- every site left open: its group, and for `gap` the reason and what pyatlan
  must add;
- whether delivered-output parity is proven or not.

## Working with a developer, and in the remediation lane

With a developer: stop after the groups are decided (before any edit), and
after the proof. In the remediation lane the decided defaults below are the
developer's answers: a group with a default is applied, a held group exits
with its brief.

## Reference shapes

String from a creator, memoized (`atlan-mysql-app` `app/mysql.py`):

```python
@lru_cache(maxsize=4096)
def _database_qn(name: str, connection_qn: str) -> str:
    qn = Database.creator(name=name, connection_qualified_name=connection_qn).qualified_name
    assert isinstance(qn, str)
    return qn
```

Only the id is known (`atlan-metabase-app` `app/qualified_names.py`):

```python
@lru_cache(maxsize=4096)
def collection_qn(connection_qn: str, collection_id: Any) -> str:
    qn = MetabaseCollection.creator(
        name=str(collection_id),
        connection_qualified_name=connection_qn,
        metabase_id=str(collection_id),
    ).qualified_name
    assert isinstance(qn, str)
    return qn
```

Process identity (`atlan-metabase-app` `app/qualified_names.py`):

```python
_UNUSED_REFS: list[Any] = [None]

def process_qn(connection_qn: str, question_id: Any, process_hash: str) -> str:
    return Process.generate_qualified_name(
        name=str(question_id),
        connection_qualified_name=connection_qn,
        inputs=_UNUSED_REFS,
        outputs=_UNUSED_REFS,
        process_id=f"question_tables/{question_id}/{process_hash}",
    )
```

Asset from a creator (`atlan-openapi-app` `app/asset_mapper.py`):

```python
def map_api_spec(record: SpecRecord, connection_qn: str) -> APISpec:
    return APISpec.creator(name=record.title, connection_qualified_name=connection_qn)
```

Child-side link (`atlan-metabase-app` `app/asset_mapper.py`):

```python
asset.metabase_collection = RelatedMetabaseCollection(qualified_name=collection_qn)
```

Serialization (`atlan-openapi-app` `app/connector.py`); attributes the model
cannot hold go on the decoded output (`atlan-metabase-app`
`serialize_entity`):

```python
ENTITY_ENVELOPE = EntityEnvelopePolicy(shape=EnvelopeShape.FLATTENED)
out_f.write(entity_bytes(asset, entity_type="api_spec", envelope=ENTITY_ENVELOPE) + b"\n")
```

Not a qualifiedName:

```python
# conformance: ignore[P028] match prefix, not a qualifiedName
prefix = f"{connection_qn}/"
```

## Decided defaults

Owner answers read by remediation tooling. `fix_shape` is the group id above.
A row with `block` holds that group for a person.

```decided-defaults
{"rule": "P028", "fix_shape": "creator-string", "decision": "Read the string from a memoized helper returning X.creator(...).qualified_name; when only the id is known pass it as name too and never emit that asset.", "why": "Reference shape: atlan-metabase-app app/qualified_names.py collection_qn; atlan-mysql-app app/mysql.py _database_qn.", "interface": false}
{"rule": "P028", "fix_shape": "creator-blank-raises", "decision": "Use the creator even when a blank or None input makes it raise; no app-side guard and no f-string fallback.", "why": "The raise replaces a broken identity such as {conn}/table/ or {conn}/None. pyatlan raises on blank identity inputs by design.", "interface": false, "pr_note": true}
{"rule": "P028", "fix_shape": "process-id", "decision": "generate_qualified_name(name=..., connection_qualified_name=..., inputs=[None], outputs=[None], process_id=<everything after the connection qualifiedName>) for Process, ColumnProcess and BIProcess.", "why": "Returns {connection_qualified_name}/{process_id}, the same string by construction. Reference shape: atlan-metabase-app app/qualified_names.py process_qn.", "interface": false, "pr_note": true}
{"rule": "P028", "fix_shape": "not-identity-ignore", "decision": "# conformance: ignore[P028] <kind>, not a qualifiedName on the line above, where kind is match prefix, object-store key, lookup key or message text; only after following every reader of the value.", "why": "The rule's terminal state allows an ignore only for a string that is not an asset qualifiedName.", "suppress": true, "reason": "not a qualifiedName", "interface": false}
{"rule": "P028", "fix_shape": "gap", "decision": "Leave the f-string in one helper per name shape, unsuppressed; do not change the string.", "why": "No creator can produce this identity for an input the source can send; changing the string changes asset identity and suppressing hides the gap."}
{"rule": "P028", "match": "\\bhash\\(", "block": "The qualifiedName is built from Python's builtin hash(), which changes between interpreter processes, so the identity is not stable today. Any stable replacement changes asset identity.", "brief": "Pick the stable id that replaces hash() and accept the one-time identity change; until then the site stays open."}
{"rule": "P028", "fix_shape": "^gap$", "block": "No creator produces this qualifiedName for an input the source can send: no-creator, differs, missing-input or rejects-input. Changing the string changes asset identity.", "brief": "Add or fix the creator in pyatlan for the type and input named in the site's reason, then apply the creator fix to this site."}
{"rule": "P028", "fix_shape": "^creator-string$", "block": "The app's lock resolves pyatlan below the version that carries the creators these sites need.", "brief": "Raise pyatlan to a version with these creators (a dependency update), then apply the creator fix.", "requires": {"package": "pyatlan", "min": "11.5.0"}}
{"rule": "P028", "fix_shape": "^creator-blank-raises$", "block": "The app's lock resolves pyatlan below the version that carries the creators these sites need.", "brief": "Raise pyatlan to a version with these creators (a dependency update), then apply the creator fix.", "requires": {"package": "pyatlan", "min": "11.5.0"}}
{"rule": "P052", "fix_shape": "reshaped-flattened", "decision": "entity_bytes(asset, envelope=EntityEnvelopePolicy(shape=EnvelopeShape.FLATTENED)); delete only the code that moves relationshipAttributes into attributes; keep every app-specific change as a step on the decoded entity_bytes output.", "why": "The hand reshape rebuilds the flattened envelope: on a pyatlan_v9 Table, entity_bytes FLATTENED equals it except connectionName and status ACTIVE added and the placeholder guid removed.", "interface": false, "pr_note": true}
{"rule": "P052", "fix_shape": "nested-released", "decision": "entity_bytes(asset, envelope=EntityEnvelopePolicy(shape=EnvelopeShape.PYATLAN)) where the released output keeps a top-level relationshipAttributes key.", "why": "Keeps the released wire shape; PYATLAN is a deprecated migration lever removed in v4.0.", "interface": false, "pr_note": true}
{"rule": "P058", "fix_shape": "asset-creator", "decision": "Build the entity with the type's pyatlan_v9 creator plus model fields and write it with entity_bytes; a key the model cannot hold goes back on the decoded output.", "why": "Reference shape: atlan-openapi-app app/asset_mapper.py and app/connector.py; atlan-metabase-app serialize_entity.", "pr_note": true}
{"rule": "P058", "fix_shape": "asset-no-creator", "decision": "Build the pyatlan_v9 model class directly with the dict's keys and write it with entity_bytes.", "why": "The type is modeled, so the model checks the keys; it has no creator, so its qualifiedName follows the P028 rules for the site that builds it.", "pr_note": true}
{"rule": "P058", "fix_shape": "process-asset", "decision": "Process.creator (or ColumnProcess / BIProcess) with process_id=<everything after the connection qualifiedName> and typed input/output references, written with entity_bytes, in the same unit as the P028 process-id sites of that identity.", "why": "generate_qualified_name and the creator return {connection_qualified_name}/{process_id}, the same identity by construction.", "pr_note": true}
{"rule": "P058", "fix_shape": "unmodeled-ignore", "decision": "Keep the dict with top-level \"status\": \"ACTIVE\" on every non-delete upsert and add # conformance: ignore[P058] <type> is not modeled in pyatlan_v9.", "why": "The rule's terminal state allows an ignore only for a type pyatlan_v9 does not model.", "suppress": true, "reason": "is not modeled in pyatlan_v9"}
{"rule": "P058", "fix_shape": "^api-patch$", "block": "The dict is a partial update sent straight to the Atlan API, not publish output; entity_bytes would add connectionName, lastSync and status to the patch.", "brief": "Decide whether a partial-update payload is in P058's scope; if it is, name the pyatlan_v9 shape it should use (for example the type's updater())."}
{"rule": "P058", "fix_shape": "^nondata-lane$", "block": "The dict is staged for publish's non-data lane, which keys entities by its own path, not by qualifiedName.", "brief": "Confirm the non-data lane accepts entity_bytes output for this type, and which identity it must carry."}
{"rule": "O004", "fix_shape": "import-only", "decision": "Import from pyatlan_v9.model.assets; every construction keyword exists on the v9 class and no legacy method is used.", "why": "The v9 classes carry the same fields; the import is the whole change.", "interface": false}
{"rule": "O004", "fix_shape": "with-serialization", "decision": "Move the import in the same unit as the mapper's P052 and P058 sites.", "why": "pyatlan_v9 rejects attributes= and has no to_atlas_dict(), so the import alone breaks the mapper."}
{"rule": "O004", "fix_shape": "legacy-pin", "decision": "# conformance: ignore[O004] naming the pin, where the app keeps the legacy AtlasTransformer on purpose.", "why": "The pin is the owner's choice and the ignore records it.", "suppress": true, "reason": "legacy AtlasTransformer pin"}
{"rule": "O004", "fix_shape": "^search-client$", "block": "The legacy models feed a search or lookup through the legacy client; moving them means moving to the pyatlan_v9 client and its FluentSearch.", "brief": "Confirm the pyatlan_v9 client is the supported search path for apps, then move the search and its models together."}
```
