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
  (O004). The skill inventories every site, decides per site whether a
  creator owns the grammar (rewrite), no creator owns it (centralise into
  one module plus a justified ignore), or the change would alter asset
  identity or wire shape (owner decision), then applies the agreed changes
  and proves them with re-detection, the test suite and an output diff.
  The done-bar is identity parity: every qualifiedName the app emits after
  the change equals the one it emitted before, unless the developer
  explicitly accepts the change. An app whose mappers emit plain dicts (no
  pyatlan import) gets P028 only: creators produce the strings, the dict shape
  stays. Run after migrate-off-daft when the app is below SDK 3.20.0;
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
last_updated: "2026-10-05"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app code with qualifiedNames taken from pyatlan_v9 creators, or centralised in one qualified-names module with a justified ignore per helper
  - app code that serializes every entity with entity_bytes(asset, envelope=...)
  - asset mappers with return annotations; asset imports from pyatlan_v9.model.assets
  - pyproject.toml and uv.lock (SDK raised to >= 3.39.0 only when creators are adopted and the lock is below it)
  - updated tests, including a qualifiedName parity test over a recorded fixture
---

# Migrate asset modeling (P028, P052, O002, O003, O004)

## Conformance rules this skill clears

P028, P052, O002, O003, O004. These rules name this skill as their
`remediation_reference`, and `/remediate` hands their findings here. When the
skill is done, run
`atlan-application-sdk-conformance detect --rule P028,P052,O002,O003,O004` and
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
| P028 | an f-string that interpolates a name matching `qualified_name`, `*_qn` or `qn` **and** has a `/` literal (an object-store key, with a `/` literal before the name, is exempt) | the qualifiedName comes from `X.creator(...).qualified_name` or `Process.generate_qualified_name(...)`, or the site carries a justified ignore |
| P052 | `.to_nested_bytes()`, `.to_nested_dict()`, pyatlan_v9 `to_atlas_format`, or `to_atlas_format_dict`, in `app/` (not `app/generated/`) | the entity goes through `entity_bytes` |
| O002 | any `.dict()` call in a module that imports pyatlan asset models | `.dict()` is gone, or the module no longer imports asset models |
| O003 | a function with no return annotation that returns a directly constructed asset `X(...)` | the function has a return annotation |
| O004 | an import of `pyatlan.model.assets` | the import is `pyatlan_v9.model.assets` |

Scope: P028 and the O-rules scan every non-test Python file (`main.py` and
`scripts/` too); P052 scans `app/` only. A suppression directive must be on
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

No creator owns the grammar — one module, one justified ignore per helper,
directly above the f-string (`atlan-metabase-app` `app/qualified_names.py`):

```python
def bi_process_qn(connection_qn: str, question_id: Any) -> str:
    # conformance: ignore[P028] bespoke BIProcess qualifiedName (questions_dashboards/{id}) — no pyatlan_v9 creator owns this grammar; centralised here as the single source of truth.
    return f"{connection_qn}/questions_dashboards/{question_id}"
```

Process identity — `Process.generate_qualified_name(..., process_id=...)`
returns `f"{connection_qualified_name}/{process_id}"`.

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
     `curl -s https://pypi.org/pypi/atlan-application-sdk/json | jq -r '.releases | to_entries[] | "\(.key) \(.value[0].upload_time)"'`.
     Creators set a random placeholder guid on every call, and only
     `entity_bytes` from 3.39.0 removes it; without it, publish sees every
     entity as changed on every run. An app that only reads `.qualified_name`
     strings from creators does not need the raise.
   - Below 3.20.0: stop, and run `migrate-off-daft` first.
2. Record the baseline: run
   `atlan-application-sdk-conformance detect --rule P028,P052,O002,O003,O004 --exit-zero --output "$TMPDIR/before.sarif"`
   and the test suite. Record the tests that already fail; they do not block
   this skill, and they must not get worse.
3. Capture the app's current output for parity. Run the offline (recorded
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

- **creator** — a `pyatlan_v9.model.assets` class with a `creator()` produces
  the **same** string. Check it: build one value both ways and compare, with
  a realistic input **and** with every edge input the app can receive: an id
  that is `None` or empty, an empty connection qualifiedName, and the real
  connection qualifiedName format. An input is reachable unless a guard or a
  required non-empty type stops it before the site; `.get(k, "")`,
  `.get(k)` and a field declared `str = ""` all make the empty value
  reachable. When the qualifiedName is built through a chain of creators,
  check every id in the chain, not only the last one: each goes in as a
  `name`. Creators enforce segment counts (`Database` needs a 3-segment
  connection qualifiedName, `Schema` 4, `Table`/`View`/`Procedure` 5) and
  raise `ValueError` where an f-string would produce `…/None` or `//`. If a
  creator can raise on any input the app accepts, the site is an **identity
  change**, whether the error drops the entity, fails the task or fails the
  run. When the app keys a name on an id, pass the id as `name`
  to get the **string** only; never emit that creator-built asset, because
  its `name` is then the id. Some families have no creators at all; check per
  asset type.
- **process** — a Process or ColumnProcess identity with the grammar
  `{connection_qn}/{process_id}`:
  `Process.generate_qualified_name(name=..., connection_qualified_name=..., inputs=<non-empty>, outputs=<non-empty>, process_id=...)`.
  Without `process_id` it returns a hash, not the app's grammar; with an
  empty `inputs` or `outputs` it raises.
- **decode** — code that takes a qualifiedName apart (`split("/")`,
  `"/".join(parts[:-1])`). It does not build an identity, so it is not a
  P028 site. Keep it unchanged, or derive the ids from the source record
  instead; never re-encode the grammar there.
- **centralise** — no creator owns the grammar. Move every such f-string into
  one `app/qualified_names.py` with one helper per shape and one justified
  ignore per helper.
- **identity change** — the only creator produces a different string. This
  changes asset identity (duplicates or orphaned assets). Owner decision; do
  not apply.

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
   sites, and the f-string helpers with their ignores for the **centralise**
   sites. Call sites use the helpers. In a dict-mapper app the helpers return
   strings only, and the emitted dicts keep their shape. Leave **identity
   change** and **decode** sites untouched and **unsuppressed**: they stay
   P028 findings, listed in the PR description, until the owner decides (for
   example to reject blank ids in `run()`, which makes the creator rewrite
   safe).
4. **P052 / O002** — serialize every entity with
   `entity_bytes(asset, envelope=ENTITY_ENVELOPE, entity_type=...)`, one module-level
   policy. Pass `connection_name=` and `last_sync=resolve_last_sync_details()`
   (`application_sdk.common.last_sync`) unless the mapper already stamps
   them. `entity_bytes` returns bytes: the sink must accept bytes (JSONL).
   A `.dict()` on a model that is not an asset is a false positive: ignore it
   with that reason.

## Step 3 — Prove it

1. `atlan-application-sdk-conformance detect --rule P028,P052,O002,O003,O004 --exit-zero --output <file>`
   — no finding left except the agreed ignores and the owner-decision sites
   (expected, unsuppressed). The checker does not see the hand-built helpers
   from Step 1 (for example a `_qn(*parts)` join): grep their call sites and
   confirm each is migrated or listed as an owner decision.
2. The test suite passes, with no failure that was not in the baseline.
3. Parity: regenerate the output from Step 0.3 and compare, as multisets
   (counts, not sets: an anonymised fixture can repeat a qualifiedName), both
   the `(typeName, qualifiedName)` pairs **and** every
   `uniqueAttributes.qualifiedName` in relationship references — a mapper
   that builds a reference changes identity as surely as one that builds an
   entity. Every value must be present before and after, with the same count.
   Ignore volatile fields such as `lastSyncRunAt`. On the `entity_bytes`
   path, `connectionName` added and placeholder guids removed are expected;
   anything else is a defect unless the developer accepts it.
4. Pin the parity in the repo: commit the before-snapshot as JSON next to the
   recorded fixture, assert `Counter` equality against it in the offline
   test, and change one grammar on purpose to confirm the test fails, then
   revert.

**Stop 2** (see Agent protocol).

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site, its class, the grammar
   comparison for each creator rewrite, the envelope for each serialization
   site, and the list of owner decisions. No edits yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   the parity diff and the new parity test. Then hand off for PR review.

If the app is below SDK 3.20.0, run `migrate-off-daft` first: this skill can
raise the SDK.
