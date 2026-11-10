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

1. **Identity.** A qualifiedName is the asset: a changed string is a new
   asset, with duplicates on the next run and orphans for everything linked
   to the old one. The grammar belongs to pyatlan's `creator()` factories, so
   one fix there reaches every app. Use a creator only where it gives the same
   string. A P028 finding the app cannot close yet is how the fleet finds a
   missing creator: never hide it.
2. **Published output.** What the app publishes stays the same, value by
   value (key order is not output), except the stamps `entity_bytes` adds by
   design: `connectionName`, top-level `status: ACTIVE`, the placeholder guid
   every creator sets removed (SDK 3.39.0). Those exist so every app gets
   central fixes, such as a soft-deleted entity that comes back being
   restored. Any other change, such as a record a guard no longer publishes,
   is listed per site in the PR for a person to accept.
3. **`entity_bytes` is the seam for publish output only.** A payload that
   something else reads (the Atlan API, another publish lane, another
   service) is not fixed by moving it onto this seam.
4. **Inputs are judged by their source.** An input matters when a real
   source can send it. Name where every input you test comes from; never
   make one up. Follow a value back to its producer (a guard, a validated
   type, the code that builds it). A value that comes from the source with
   no check has the format the repo's fixtures, tests and golden data show;
   only evidence of another format (a recorded fixture, a spec, a bug report)
   makes a creator's rejection of it matter. Two cases differ:
   - a **blank** (an empty string or `None` at the site, nothing else) already
     published a broken identity (`{conn}//{schema}`, `…/None`), so it gets
     an app guard (FND-3887), never a fallback string. The guard uses the
     app's existing bad-record handling; if the app has none, it skips the
     record and logs a warning that names it (lane owner's decision,
     2026-11-10: it keeps the app's fail-soft behaviour, and an asset with an
     empty name part was never valid). Each guard and what it does with the
     record is listed per site in the PR for the app owner to accept. Where
     the asset is valid without that value (a Table with no
     schema), the creator must accept it; if the installed creator raises or
     returns another string, the identity would change (principle 1): the
     site is a gap, and the existing string stays;
   - a **valid** value the creator rejects published a correct asset: it
     must never be lost quietly. With evidence that the source sends it, the
     site is a gap. Without evidence, say what a raise does in this app: if
     the app catches it per record, the asset would vanish, so the site is a
     gap; only where the raise fails the run visibly does the creator fix go
     ahead.

   Existing test inputs are evidence of what the source sends. Never change
   them to make a creator accept a record: that hides the output change
   instead of proving there is none. Optional parents come from what the
   source record carries.
5. **When 1 or 2 cannot be proven, hold the site and say what is missing.**
   Plan it as type `SDK` when pyatlan or the SDK must add something (name
   the type, the creator, the input), or as type `Decision` when an owner
   must choose (ask the question). Your plan text is what the person reads.
   A site whose shape this skill does not describe gets the same treatment:
   describe the shape in the PR as a candidate for this skill.

The rule definitions own what "correct" is: each finding carries its rule's
`full_description` and `terminal_state`. This skill owns how to apply them
without breaking 1 and 2, and how to prove it.

## Facts you need

- Check creators in the installed pyatlan, never from memory:
  `"creator" in vars(X)` (Process, ColumnProcess, BIProcess:
  `"generate_qualified_name" in vars(X)`). An inherited creator is not this
  type's creator. A type without its own creator can be built directly.
- Creators enforce segment counts (`Database` needs a 3-segment connection
  qualifiedName, `Schema` 4, `Table`/`View`/`Procedure` 5); some need
  `connection_qualified_name=` to accept a non-standard depth. For a chain of
  creators, every id in the chain goes in as a `name`.
- `Process.generate_qualified_name(..., process_id=X)` returns
  `{connection_qn}/X`; `process_id=None` falls back to a hash, a different
  identity. `Process.creator` raises on an empty `inputs` or `outputs` list.
- `EnvelopeShape.FLATTENED` puts relationship references under `attributes`;
  `EnvelopeShape.PYATLAN` (deprecated, removed in v4.0) keeps them under a
  top-level `relationshipAttributes`. Code that decodes `to_nested_bytes()`
  and moves `relationshipAttributes` into `attributes` publishes
  `FLATTENED`. Judge the envelope by what the app publishes, never by the
  method it calls.
- `FLATTENED` is known to differ from a hand reshape of nested output in at
  least these places, which a unit test rarely reaches (observed on seven
  apps, not a complete list): a reference set to `None` is written as
  `attributes.<ref>: null`, which clears that relationship on publish (the
  nested encoder omits it); `meanings` stays top-level only and loses
  `semantic` (nested output also carries it under `relationshipAttributes`);
  every set reference is emitted, including ones the app used to drop. Both
  encoders omit a field that was never set (`entity_envelope` module docs):
  leave a reference unset when the source has none, never assign `None`.
  For `meanings` and dropped references this skill has no tested answer:
  where they appear, hold the site. Any other difference in what the app
  publishes is not allowed either (principle 2): where you find one, hold
  the site and name it.
- A reference built with `qualified_name=` gains an extra top-level
  `qualifiedName` key; `unique_attributes={"qualifiedName": ...}` writes the
  usual `{typeName, uniqueAttributes}` reference.
- A key the model cannot hold goes on the decoded `entity_bytes` output
  (`atlan-metabase-app` `serialize_entity`).
- Call `resolve_last_sync_details()` (`application_sdk.common.last_sync`)
  once per transform activity, outside the per-record loop, and pass it as
  `last_sync=`; pass `connection_name=` unless the mapper stamps it.
  `entity_bytes` returns bytes: the sink must accept bytes.

## What each rule fires on

| Rule | Fires on | Stops when |
|---|---|---|
| P028 | an f-string that interpolates a name matching `qualified_name`, `*_qn` or `qn` **and** has a `/` literal (an object-store key, with a `/` literal before the name, is exempt) | the qualifiedName comes from `X.creator(...).qualified_name` or `Process.generate_qualified_name(...)`; an ignore is correct only where the string is not a qualifiedName (the rule's terminal state) |
| P052 | `.to_nested_bytes()`, `.to_nested_dict()`, pyatlan_v9 `to_atlas_format`, or `to_atlas_format_dict`, in `app/` (not `app/generated/`) | the entity goes through `entity_bytes` |
| P055 | a pyatlan_v9 mapper populating the list end of a 1-to-N relationship — `Table(columns=...)`, `process.fabric_activities = [...]`, `schema.tables.append(...)` | each child sets its single reference to the parent (`column.table = RelatedTable(...)`) and the parent's list is left unset |
| P058 | a dict with literal `"typeName"` and `"attributes"` keys (or `dict(typeName=..., attributes=...)`) that is appended / extended / inserted, yielded, passed to `entity_bytes`, or returned from a `map_<entity>` mapper, in `app/` (not `app/generated/`) — directly or through a local name | the entity is a `pyatlan_v9.model.assets` object, or the site carries a justified ignore for a type pyatlan_v9 does not model |
| O002 | any `.dict()` call in a module that imports pyatlan asset models | `.dict()` is gone, or the module no longer imports asset models |
| O003 | a function with no return annotation that returns a directly constructed asset `X(...)` | the function has a return annotation |
| O004 | an import of `pyatlan.model.assets` | the import is `pyatlan_v9.model.assets` |

Scope: P028, P055 and the O-rules scan every non-test Python file (`main.py` and
`scripts/` too); P052 and P058 scan `app/` only. A suppression directive must be on
the finding's line or on a comment-only line directly above it.

**Do not dodge P028.** The checker does not see `"/".join(...)`, `+`,
`.format()`, `%`, renamed variables, or the deprecated
`application_sdk.transformers.common.utils.build_atlas_qualified_name`. Using
any of them clears the finding and keeps the hand-built grammar. That is not a
fix. If the app already has such helpers (for example a `_qn(*parts)` join),
treat their call sites as part of this migration.

## The rules

### P028 — identity from creators

End state per site, from the rule's `full_description`:

- a creator gives the same string: read it from a memoized
  `X.creator(...).qualified_name` helper; when only the id is known, pass it
  as `name` too and never emit that asset;
- the same, but the creator raises on a blank input (pyatlan does, on
  purpose): the guard principle 4 describes, never a fallback string; list
  each guard and what it does with the record in the PR;
- a Process, ColumnProcess or BIProcess `{connection_qn}/{suffix}`:
  `generate_qualified_name(..., process_id=<suffix>)`;
- the string never reaches a `qualifiedName` field or a reference (follow
  every reader): `# conformance: ignore[P028] <kind>, not a qualifiedName`;
- code that splits a qualifiedName apart: not a P028 site, leave it;
- no creator gives the same string for an input the source can send: a gap.
  The f-string stays in one helper per name shape, unsuppressed. Say which:
  the type has no creator, the creator gives another string, it needs an
  input the source record lacks, or it rejects a valid value the source
  sends (with evidence, principle 4). A gap claim is a fact someone can
  check, so it carries its proof: the result of the creator check on the
  installed pyatlan; both strings for one real input; the creator argument
  the record lacks; or the value, its source and the error. A gap claim
  without its proof is not a gap: hold the site and say what is unknown.

### P052 — one serialization seam

Write every entity through `entity_bytes`, with the envelope that keeps what
the app publishes (principle 2). Delete only the code that rebuilds that
envelope by hand; any other change the app makes to the output stays, applied
to the decoded `entity_bytes` output. A helper whose output is not an entity
envelope at all is principle 3 or 5.

### P058 — entities as pyatlan_v9 assets

A dict has no model checking its keys and no creator owning its identity, and
outside `entity_bytes` it misses `status: ACTIVE`, so a soft-deleted entity
that comes back stays deleted. End state: each publish entity is a pyatlan_v9
asset (creator where the type has one, P028 applies to it) written with
`entity_bytes`, with the output unchanged. A publish entity whose literal
type name pyatlan_v9 does not model (checked with
`hasattr(pyatlan_v9.model.assets, "<Type>")`) keeps the dict with top-level
`"status": "ACTIVE"` and
`# conformance: ignore[P058] <type> is not modeled in pyatlan_v9`. That
claim must be checkable: a type held in a variable, or a payload that is not
publish output (principle 3), never gets this ignore.

### O004 — pyatlan_v9 models

Import from `pyatlan_v9.model.assets`. The v9 classes reject `attributes=`
and have no `to_atlas_dict()`, and a search runs on the pyatlan_v9 client:
where the models feed a mapper, move them with that mapper's P052 and P058
sites; where they feed anything the import alone cannot move, principle 5.

### O002, O003, P055 (unchanged)

- **O002** — serialize through `entity_bytes` as for P052. A `.dict()` on a model that is not an asset is a false positive: ignore it
  with that reason.
- **O003** — add the return annotation (`-> Table`, a union, or `Optional`).
- **P055** — remove the list from the parent and set the single reference
  the finding names on each child instead (`activity.fabric_process =
  RelatedProcess(qualified_name=process_qn)`). The child's mapper must know
  its parent's qualifiedName; derive it from the child's source record, not
  from the parent asset. If a child is emitted only through the parent's
  list (no mapper of its own), that is an owner decision: the app has to
  start emitting the child.

Order inside one mapper: O004 → O003 → P028 → P052 / O002 → P055 → P058.
Sites that share a mapper or an identity move in one unit.

## Before you change anything

1. SDK version from `uv.lock`: 3.39.0 or later, or raise it when the app
   will emit creator-built assets. Below 3.20.0: stop and run
   `migrate-off-daft`.
2. A creator missing in the locked pyatlan but present in a newer pyatlan
   release inside the allowed range (pyatlan is first-party: no release-age
   wait): with a developer, raise pyatlan in the lock; in the remediation
   lane the site is held for that raise.
3. Baseline:
   `atlan-application-sdk-conformance detect --rule P028,P052,P055,P058,O002,O003,O004 --exit-zero --output before.sarif`
   and the test suite; record tests that already fail.
4. Capture today's output from the offline (recorded) integration test, one
   JSONL line per entity, outside the repo. No such test: record that
   delivered-output parity is not proven, and pin each touched type's
   qualifiedName in a unit test before any edit.

## Prove it

1. Re-detect with the baseline command: only gaps, held and ignored sites
   remain. Grep the call sites of hand-built helpers the checker cannot see
   (for example a `_qn(*parts)` join) and confirm each is migrated or held.
2. Tests: no failure that was not in the baseline.
3. Identity: the `(typeName, qualifiedName)` pairs and every
   `uniqueAttributes.qualifiedName` in references, compared as multisets
   (counts), are equal before and after. P055 moves references by design:
   compare `(parent, child)` pairs instead.
4. Output: every entity's values equal before and after, including where
   relationships sit and the shape of each reference, except the changes
   principle 2 allows and volatile `lastSync*` values. Recorded fixtures
   rarely contain the cases in the envelope facts above: add one entity with
   a reference set to `None`, one with `meanings`, and one with each
   reference the app drops, and compare those too.
5. Pin it: commit the before-snapshot next to the recorded fixture and
   assert `Counter` equality in the offline test. Break one grammar on
   purpose, see the test fail, revert.

## PR description

- the sites changed and the end state applied;
- every input that now raises;
- every output difference and why it is expected;
- every site held: what is missing, and who must add or decide it;
- whether delivered-output parity is proven or not.

## Working with a developer, and in the remediation lane

With a developer: stop after the plan (before any edit), and after the proof.
In the remediation lane the decided defaults below are the developer's
answers; a held site exits with its plan text as the brief.

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

Owner answers read by remediation tooling. A row with `block` holds the
sites planned with that `fix_shape` for a person.

```decided-defaults
{"rule": "P028", "fix_shape": "creator-string", "decision": "Read the string from a memoized helper returning X.creator(...).qualified_name; when only the id is known pass it as name too and never emit that asset.", "why": "Reference shape: atlan-metabase-app app/qualified_names.py collection_qn; atlan-mysql-app app/mysql.py _database_qn.", "interface": false}
{"rule": "P028", "fix_shape": "creator-blank-guard", "decision": "Use the creator. Guard an empty string or None input (nothing else) in the app before the creator, never with a fallback string: use the app's existing bad-record handling; if the app has none, skip the record and log a warning that names it. List each guard and what it does with the record in the PR. Where the asset is valid without the value, use the creator's optional parents as the source record carries them; if the installed creator then raises or returns another string, the site is a gap. Never change existing test inputs to make a creator accept a record.", "why": "FND-3887: blanks get an app guard. The guard action is the lane owner's decision of 2026-11-10: it keeps the app's fail-soft behaviour (the standing rule for sweep fixes), matches the approved azure-event-hub case (blank-topic rows skipped), and an asset with an empty name part was never valid; stopping a whole run for it does more harm.", "pr_note": true, "interface": false}
{"rule": "P028", "fix_shape": "process-id", "decision": "generate_qualified_name(name=..., connection_qualified_name=..., inputs=[None], outputs=[None], process_id=<everything after the connection qualifiedName>) for Process, ColumnProcess and BIProcess. A blank name or connection qualifiedName raises; guard it as for creator-blank-guard.", "why": "Returns {connection_qualified_name}/{process_id}, the same string by construction. Reference shape: atlan-metabase-app app/qualified_names.py process_qn.", "interface": false}
{"rule": "P028", "fix_shape": "not-identity-ignore", "decision": "# conformance: ignore[P028] <kind>, not a qualifiedName on the line above, where kind is match prefix, object-store key, lookup key or message text; only after following every reader of the value.", "why": "The rule's terminal state allows an ignore only for a string that is not an asset qualifiedName.", "suppress": true, "reason": "not a qualifiedName", "interface": false}
{"rule": "P028", "fix_shape": "gap", "decision": "Leave the f-string in one helper per name shape, unsuppressed; do not change the string. State which gap reason applies and its proof: the creator check on the installed pyatlan (\"creator\" in vars(X)), both strings for one real input, the creator argument the source record lacks, or the rejected value with its source and the error. Without the proof, hold the site instead.", "why": "No creator can produce this identity for a value the source is shown to send; changing the string changes asset identity and suppressing hides the gap. A gap claim is a checkable fact: in one lane run an editor claimed four types had no creator when all four had one."}
{"rule": "P028", "match": "\\bhash\\(", "block": "The qualifiedName is built from Python's builtin hash(), which changes between interpreter processes, so the identity is not stable today. Any stable replacement changes asset identity.", "brief": "Pick the stable id that replaces hash() and accept the one-time identity change; until then the site stays open."}
{"rule": "P028", "fix_shape": "^gap$", "block": "No creator produces this qualifiedName: the type has no creator in a usable pyatlan, the creator produces a different string, needs an input the source record lacks, or rejects a valid value the source is shown to send. Changing the string changes asset identity.", "brief": "Add or fix the creator in pyatlan, then apply the creator fix to this site."}
{"rule": "P028", "fix_shape": "^creator-string$", "block": "The app's lock resolves pyatlan below the version that carries the creators these sites need.", "brief": "Raise pyatlan to a version with these creators (a dependency update), then apply the creator fix.", "requires": {"package": "pyatlan", "min": "11.7.0"}}
{"rule": "P028", "fix_shape": "^creator-blank-guard$", "block": "The app's lock resolves pyatlan below the version that carries the creators these sites need.", "brief": "Raise pyatlan to a version with these creators (a dependency update), then apply the creator fix.", "requires": {"package": "pyatlan", "min": "11.7.0"}}
{"rule": "P028", "fix_shape": "^hold$", "block": "Held: this site's identity or published output cannot be proven unchanged, or its payload is not publish output. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P052", "fix_shape": "released-envelope", "decision": "Write the entity with entity_bytes and the envelope that matches what the app publishes today; delete only the code that rebuilds that envelope by hand; keep every other change the app makes, applied to the decoded entity_bytes output.", "why": "entity_bytes is the seam for central fixes; the envelope choice keeps the published output unchanged except the documented stamps.", "pr_note": true}
{"rule": "P052", "fix_shape": "^hold$", "block": "Held: this site's identity or published output cannot be proven unchanged, or its payload is not publish output. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "P058", "fix_shape": "pyatlan-asset", "decision": "Build each publish entity as a pyatlan_v9 asset (the type's creator where it has one) and write it with entity_bytes so the published output is unchanged except the documented stamps.", "why": "A model checks the keys and a creator owns the identity; outside entity_bytes the entity misses status ACTIVE.", "pr_note": true}
{"rule": "P058", "fix_shape": "unmodeled-ignore", "decision": "Only for a publish entity whose literal type name pyatlan_v9 does not model (hasattr(pyatlan_v9.model.assets, \"<Type>\") is False): keep the dict with top-level \"status\": \"ACTIVE\" on every non-delete upsert and add # conformance: ignore[P058] <type> is not modeled in pyatlan_v9. Never for a type held in a variable or a payload that is not publish output.", "why": "The rule's terminal state allows an ignore only for a type pyatlan_v9 does not model.", "suppress": true, "reason": "is not modeled in pyatlan_v9"}
{"rule": "P058", "fix_shape": "^hold$", "block": "Held: this site's identity or published output cannot be proven unchanged, or its payload is not publish output. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
{"rule": "O004", "fix_shape": "v9-import", "decision": "Import from pyatlan_v9.model.assets, moving the models together with the mapper's P052 and P058 sites when they feed a mapper.", "why": "The v9 classes reject attributes= and have no to_atlas_dict(), so the import alone breaks a mapper."}
{"rule": "O004", "fix_shape": "^hold$", "block": "Held: this site's identity or published output cannot be proven unchanged, or its payload is not publish output. The planner's reason for this site says what is missing.", "brief": "Add what the planner's reason names as missing, or answer its question, then re-run the unit."}
```
