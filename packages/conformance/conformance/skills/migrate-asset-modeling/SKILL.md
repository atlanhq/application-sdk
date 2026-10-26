---
name: migrate-asset-modeling
description: >
  Move a connector app's asset modeling onto the canonical shape the
  conformance asset-modeling rules check: qualifiedNames from pyatlan_v9
  creators instead of hand-built f-strings (P028), every entity serialized
  through the SDK's entity_bytes seam instead of to_nested_bytes /
  to_nested_dict / to_atlas_format / .dict() (P052, O002), asset mappers
  with a return annotation (O003), and asset models imported from
  pyatlan_v9.model.assets instead of the legacy pyatlan.model.assets
  (O004), and 1-to-N links written from the child's single reference rather
  than the parent's list (P055), and entities built as pyatlan_v9 assets
  rather than raw typeName/attributes dicts (P058). The skill inventories every site, decides per site whether a
  creator produces the string (rewrite), no creator produces it yet (a gap
  in pyatlan: centralise and leave the finding open), the string is not a
  qualifiedName at all (reasoned ignore), or the change would alter wire
  shape (owner decision), then applies the agreed changes
  and proves them with re-detection, the test suite and an output diff.
  The done-bar is identity parity: every qualifiedName the app emits after
  the change equals the one it emitted before, unless the developer
  explicitly accepts the change. An app whose mappers emit plain dicts (no
  pyatlan import) gets P028 and P058: creators produce the strings, and each
  emitted dict moves onto its pyatlan_v9 asset unless pyatlan_v9 does not
  model the type. Run after migrate-off-daft when the app is below SDK 3.20.0;
  adopting creators needs SDK 3.39.0 or later.
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
last_updated: "2026-10-26"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app code with qualifiedNames taken from pyatlan_v9 creators; where pyatlan has a gap, the f-string centralised in one qualified-names module and left unsuppressed
  - app code that serializes every entity with entity_bytes(asset, envelope=...)
  - asset mappers with return annotations; asset imports from pyatlan_v9.model.assets
  - asset mappers that link each child to its parent from the child side (1-to-N relationships)
  - pyproject.toml and uv.lock (SDK raised to >= 3.39.0 only when creators are adopted and the lock is below it)
  - updated tests, including a qualifiedName parity test over a recorded fixture
---

# Migrate asset modeling (P028, P052, P055, P058, O002, O003, O004)

## Conformance rules this skill clears

P028, P052, P055, P058, O002, O003, O004. These rules name this skill as their
`remediation_reference`, and `/remediate` hands their findings here. When the
skill is done, run
`atlan-application-sdk-conformance detect --rule P028,P052,P055,P058,O002,O003,O004` and
confirm none of these rule ids is still reported.

## Why this matters

A qualifiedName is an asset's identity. When an app builds it by hand, the
grammar lives in many places and drifts; pyatlan's `creator()` factories own
it in one place. `entity_bytes` is the one seam where the SDK injects
`connectionName`, applies the entity envelope and strips the placeholder guid
that every creator sets (SDK 3.39.0). An entity serialized another
way misses every central fix. Both changes can alter what the app publishes,
so the skill proves parity before it finishes.

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

## Reference shapes

String wanted — derive it from the creator, memoized on hot paths
(`atlan-mysql-app` `app/mysql.py`):

```python
from functools import lru_cache
from pyatlan_v9.model.assets import Database, Schema

@lru_cache(maxsize=4096)
def _database_qn(name: str, connection_qn: str) -> str:
    qn = Database.creator(name=name, connection_qualified_name=connection_qn).qualified_name
    assert isinstance(qn, str)
    return qn
```

Asset wanted — build it with the creator (`atlan-openapi-app`
`app/asset_mapper.py`):

```python
def map_api_spec(record: SpecRecord, connection_qn: str) -> APISpec:
    return APISpec.creator(name=record.title, connection_qualified_name=connection_qn)
```

String wanted, caller has only the id — pass the id as `name` too and never
emit that asset (`atlan-metabase-app` `app/qualified_names.py`):

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

Process identity — `process_id` is everything after the connection
qualifiedName; placeholders stand in for `inputs` / `outputs` when only ids
are known (`atlan-metabase-app` `app/qualified_names.py`):

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

Not a qualifiedName — the one case for an ignore, with the reason on the
line above:

```python
# conformance: ignore[P028] match prefix, not a qualifiedName
prefix = f"{connection_qn}/"
```

1-to-N link from the child side — the parent's list stays unset
(`atlan-metabase-app` `app/asset_mapper.py`):

```python
asset.metabase_collection = RelatedMetabaseCollection(qualified_name=collection_qn)
```

Serialization (`atlan-openapi-app` `app/connector.py`):

```python
from application_sdk.common.asset_serialization import entity_bytes
from application_sdk.common.entity_envelope import EntityEnvelopePolicy, EnvelopeShape

ENTITY_ENVELOPE = EntityEnvelopePolicy(shape=EnvelopeShape.FLATTENED)
out_f.write(entity_bytes(asset, entity_type="api_spec", envelope=ENTITY_ENVELOPE) + b"\n")
```

Attributes the v9 model cannot hold — decode the `entity_bytes` output and
add them (`atlan-metabase-app` `serialize_entity`); never fall back to
`to_nested_bytes()`, which trips P052.

## Step 0 — Preconditions

1. Read the resolved SDK version from `uv.lock`.
   - 3.39.0 or later: no change.
   - Below 3.39.0 and the app will emit creator-built **assets**: raise the
     SDK to the newest release that is at least 7 days old and at or above that floor
     (dependency cooldown; never a release younger than 7 days unless it fixes
     a known vulnerability). List releases with dates:
     `curl -s https://pypi.org/pypi/atlan-application-sdk/json | jq -r '.releases | to_entries[] | select((.value | length) > 0 and (.value[0].yanked | not)) | "\(.key) \(.value[0].upload_time)"'`
     (yanked releases are skipped).
     Creators set a random placeholder guid on every call, and only
     `entity_bytes` from 3.39.0 removes it; without it, publish sees every
     entity as changed on every run. An app that only reads `.qualified_name`
     strings from creators does not need the raise.
   - Below 3.20.0: stop, and run `migrate-off-daft` first.
2. Check the creators in the pyatlan the app resolves. For every asset type
   at a P028 site, run
   `uv run python -c 'from pyatlan_v9.model.assets import X; print("creator" in vars(X))'`
   (Process, ColumnProcess and BIProcess: `"generate_qualified_name" in vars(X)`).
   A type prints `False` when it has no creator in that version. If a newer
   pyatlan release that is at least 7 days old and inside the app's allowed
   range has it, upgrade pyatlan in the lock (no constraint change). If no
   such release has it, the site is a **gap** (Step 1): it waits for pyatlan,
   so do not plan it.
3. Record the baseline: run
   `atlan-application-sdk-conformance detect --rule P028,P052,P055,P058,O002,O003,O004 --exit-zero --output "$TMPDIR/before.sarif"`
   and the test suite. Record the tests that already fail; they do not block
   this skill, and they must not get worse.
4. Capture the app's current output for parity. Run the offline (recorded
   or cassette) integration test with `--basetemp=<scratch dir>` and collect
   the transformed entity files, one JSONL line per entity, outside the repo.
   If the app has no such test, stop and ask the developer how to produce the
   output. Check coverage: the capture must contain every asset type and
   process kind (each distinct process qualifiedName grammar, for example
   `{conn}/{model}@allmodule`) in the Step 1 inventory. For a type it does not
   contain, first check whether an existing unit test already pins its
   qualifiedName; if none does, add one for a sample input before any edit.

## Step 1 — Inventory and classify every site

For each finding, and for every hand-built qualifiedName the checker cannot
see, record the asset type, the current grammar and one class. One row per
finding; unseen sites that call the same helper share one row that names the
helper and lists their lines.

The fix for each class is in the P028 rule definition (`full_description`
and `terminal_state`); this step only decides the class.

- **creator** — a `pyatlan_v9.model.assets` class with a `creator()` produces
  the **same** string. Check it: build one value both ways with a realistic
  input and compare. When the qualifiedName is built through a chain of
  creators, check every id in the chain, not only the last one: each goes in
  as a `name`. Creators enforce segment counts (`Database` needs a 3-segment
  connection qualifiedName, `Schema` 4, `Table`/`View`/`Procedure` 5); some
  need `connection_qualified_name=` to accept a non-standard depth. When the
  app keys a name on an id, pass the id as `name` to get the **string** only;
  never emit that creator-built asset, because its `name` is then the id.
  Then check the edge inputs the app can receive: an id that is `None` or
  empty, and an empty connection qualifiedName. An input is reachable unless
  a guard or a required non-empty type stops it before the site;
  `.get(k, "")`, `.get(k)` and a field declared `str = ""` all make the empty
  value reachable. Where the creator raises on a reachable blank, the site is
  still **creator**: the raise replaces a qualifiedName like `…/None` or
  `…/` and is the intended behaviour. Record each such input for the PR
  description.
- **process** — a Process, ColumnProcess or BIProcess identity with the
  grammar `{connection_qn}/{suffix}`: `generate_qualified_name(...,
  process_id=<suffix>)` returns the same string by construction.
- **decode** — code that takes a qualifiedName apart (`split("/")`,
  `"/".join(parts[:-1])`). It does not build an identity, so it is not a
  P028 site. Keep it unchanged, or derive the ids from the source record
  instead; never re-encode the grammar there.
- **not an identity** — the f-string is an object-store key or prefix, a
  `startswith` or prefix match, a cache or lookup key, or log, report or
  display text. Prove it: follow the value to every reader and confirm none
  puts it in a `qualifiedName` field or a relationship reference. A value
  that does reach one is not this class.
- **gap** — no creator for the type in any usable pyatlan (Step 0.2), a
  creator that produces a different string for a realistic input, a creator
  that needs an input the app does not have (for example `order` or a parent
  name), or a process identity not rooted at the connection qualifiedName.
  It waits for pyatlan; changing the string would change asset identity
  (duplicates or orphaned assets).

For P052, O002 and O004 sites, record the serialization call and the model
generation. For P052, the envelope keeps the released wire shape:
`to_nested_bytes()` / `to_nested_dict()` ⇒ `EnvelopeShape.PYATLAN` (deprecated,
removed in v4.0); `to_atlas_format()` ⇒ `EnvelopeShape.FLATTENED`. Moving to
`FLATTENED` from a nested shape changes the wire shape: owner decision.

**Stop 1** (see Agent protocol).

## Step 2 — Apply, in this order

1. **O004** — change the imports to `pyatlan_v9.model.assets`. Check every
   construction site: the keyword arguments must exist on the v9 class.
   Sites that do not match go to the developer, not to a guess. An app
   pinned on purpose to the legacy `AtlasTransformer` keeps the import with
   an ignore that names that pin.
2. **O003** — add the return annotation (`-> Table`, a union, or `Optional`).
3. **P028** — put every qualifiedName helper in one `app/qualified_names.py`:
   `lru_cache` helpers that return `X.creator(...).qualified_name` for the
   **creator** sites, `generate_qualified_name` helpers for the **process**
   sites, and the f-string helpers, unsuppressed, for the **gap** sites. Call
   sites use the helpers. In a dict-mapper app the helpers return strings
   only, and the emitted dicts keep their shape. Do not add a guard in front
   of a creator that raises on a blank input. Put the ignore from the rule's
   terminal state on each **not an identity** site. Leave **decode** sites
   untouched. **gap** sites stay P028 findings, listed in the PR description
   with the reason, until pyatlan fills the gap.
4. **P052 / O002** — serialize every entity with
   `entity_bytes(asset, envelope=ENTITY_ENVELOPE, entity_type=...)`, one module-level
   policy. Pass `connection_name=` and `last_sync=` unless the mapper already
   stamps them. Call `resolve_last_sync_details()`
   (`application_sdk.common.last_sync`) once per transform activity, outside
   the per-record loop, and pass that same value for every entity: each call
   reads the current time, so a per-record call gives one run's entities
   different `lastSyncRunAt` values. `entity_bytes` returns bytes: the sink must accept bytes (JSONL).
   A `.dict()` on a model that is not an asset is a false positive: ignore it
   with that reason.
5. **P055** — remove the list from the parent and set the single reference
   the finding names on each child instead (`activity.fabric_process =
   RelatedProcess(qualified_name=process_qn)`). The child's mapper must know
   its parent's qualifiedName; derive it from the child's source record, not
   from the parent asset. If a child is emitted only through the parent's
   list (no mapper of its own), that is an owner decision: the app has to
   start emitting the child.
6. **P058** — rebuild each emitted dict with the `pyatlan_v9.model.assets`
   creator for its type and serialize it as in step 4. The keys the dict
   carried become creator arguments or model fields; a key the model cannot
   hold goes back on the line `entity_bytes` produced, as in step 4. Where
   pyatlan_v9 does not model the type, keep the dict, give it top-level
   `"status": "ACTIVE"` on every non-delete upsert, and add
   `# conformance: ignore[P058] <reason>` naming the type. A dict mapper
   moved onto `entity_bytes` gains `status`, `connectionName` and the
   declared envelope: list those in the parity check (Step 3) as expected.

## Step 3 — Prove it

1. `atlan-application-sdk-conformance detect --rule P028,P052,P055,P058,O002,O003,O004 --exit-zero --output <file>`
   — no finding left except the **not an identity** ignores, the **gap**
   sites and the owner-decision sites (expected, unsuppressed). The checker
   does not see the hand-built helpers from Step 1 (for example a
   `_qn(*parts)` join): grep their call sites and confirm each is migrated or
   listed as a gap or an owner decision.
2. The test suite passes, with no failure that was not in the baseline.
3. Parity: regenerate the output from Step 0.4 and compare, as multisets
   (counts, not sets: an anonymised fixture can repeat a qualifiedName), both
   the `(typeName, qualifiedName)` pairs **and** every
   `uniqueAttributes.qualifiedName` in relationship references — a mapper
   that builds a reference changes identity as surely as one that builds an
   entity. Every value must be present before and after, with the same count.
   Ignore volatile fields such as `lastSyncRunAt`. On the `entity_bytes`
   path, `connectionName` added and placeholder guids removed are expected;
   anything else is a defect unless the developer accepts it. P055 fixes
   move references by design: each parent loses its list of child
   references and each child gains one reference to its parent. Compare
   those as the set of `(parent, child)` pairs, which must be equal before
   and after.
4. Pin the parity in the repo: commit the before-snapshot as JSON next to the
   recorded fixture, assert `Counter` equality against it in the offline
   test, and change one grammar on purpose to confirm the test fails, then
   revert.

**Stop 2** (see Agent protocol).

## Decided defaults

Owner answers for P028 sites, read by remediation tooling. Each row names the
class from Step 1 as its `fix_shape`.

```decided-defaults
{"rule": "P028", "fix_shape": "creator-string", "decision": "Read the string from a memoized helper returning X.creator(...).qualified_name; when only the id is known pass it as name too and never emit that asset.", "why": "Reference shape: atlan-metabase-app app/qualified_names.py collection_qn; atlan-mysql-app app/mysql.py _database_qn."}
{"rule": "P028", "fix_shape": "creator-blank-raises", "decision": "Use the creator even when a reachable blank or None input makes it raise; add no app-side guard and keep no f-string fallback. List each such input in the PR description.", "why": "The raise replaces a broken identity such as {conn}/table/ or {conn}/None. pyatlan raises on blank identity inputs by design; a blank made by app code is an app bug, a blank the source sends is a pyatlan gap."}
{"rule": "P028", "fix_shape": "process-id", "decision": "generate_qualified_name(name=..., connection_qualified_name=..., inputs=[None], outputs=[None], process_id=<everything after the connection qualifiedName>) for Process, ColumnProcess and BIProcess.", "why": "Returns {connection_qualified_name}/{process_id}, the same string by construction. Reference shape: atlan-metabase-app app/qualified_names.py process_qn."}
{"rule": "P028", "fix_shape": "not-identity-ignore", "decision": "# conformance: ignore[P028] <kind>, not a qualifiedName on the line above, where kind is match prefix, object-store key, lookup key or message text; only after following every reader of the value.", "why": "The rule's terminal state allows an ignore only for a string that is not an asset qualifiedName."}
{"rule": "P028", "fix_shape": "gap", "decision": "Leave the f-string in one helper per name shape, unsuppressed, and list it in the PR description; do not change the string.", "why": "No creator, a different string, or a missing creator input is a pyatlan gap; changing the string changes asset identity and suppressing hides the gap."}
```

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site, its class, the grammar
   comparison for each creator rewrite, the envelope for each serialization
   site, and the list of owner decisions. No edits yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   the parity diff and the new parity test. Then hand off for PR review.

If the app is below SDK 3.20.0, run `migrate-off-daft` first: this skill can
raise the SDK.
