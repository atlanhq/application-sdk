---
kind: responsibility
name: optimizations-area
description: >
  Maintains the current O-series violation-set and drives remediation of
  optimisation / recommendation findings.  Fully implemented: O-series fixes
  are judgment edits with a gate that bites (behavioural tests catch a
  bytes/str regression), so the bounded loop is safe to run here.
---

### Maintains

The current set of unsuppressed O-series (optimisation) conformance findings
in the working tree, classified by disposition (FAILING / WARNING) and
remediability.

#### violations-optimizations

The fingerprint-set of all unsuppressed FAILING O-series results in the
current working tree, as reported by `suite.runner --series O`.

O-series rules are WARN-tier, so in **default** mode this facet is typically
empty (warnings do not fail the gate).  In **strict** mode the fingerprint-set
includes unsuppressed WARNING results, which is where O001 remediation
actually runs.

This facet's fingerprint moves when any O-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series O` exits 0
> (zero unsuppressed FAILING results).  In strict mode, additionally: the
> `atlan/summary.warning` count for O-series in the SARIF output is 0 (every
> O-series WARNING was cleared by a real fix or a justified suppression).

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
This is the Reactor-ready wake source — in the Claude Code skill path, the
skill caller re-invokes on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "O"
  rule_ids: rule_ids
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "optimizations"`._

Consult the finding's `hint` and `message`, then look at the actual source
lines around `finding.line` in `finding.file` before proposing a fix.

**Judgment rules** (`classification = "judgment"`; always route to residue).
Two flags apply in this area: **O001 and O005 are `autofixable = true`** — the
lane applies the prescription below and residues the result for audit —
while **O002, O003, O004 and O006 are `autofixable = false`**, *migration*
rules: for those, apply nothing and return `not_remediable = true` with a
`migration_brief` built from the reference app named by
`finding.canonical_reference` (see `remediate-finding`'s *Reference apps,
impact analysis and verification*).  Produce a `"fix"` outcome only for the
two auto-fixable rules:

- **O001 OrjsonOverStdlibJson** — the site calls `json.dumps(...)` or
  `json.loads(...)` on the stdlib module.  `orjson` is **not** a drop-in, so
  this is never mechanical:
  - `json.loads(s)` → `orjson.loads(s)` is usually direct (orjson accepts
    `str` or `bytes`).
  - `json.dumps(obj)` → `orjson.dumps(obj)` returns **`bytes`, not `str`**.
    Inspect the call site: if the result is written to a text sink, passed
    where a `str` is required, or concatenated with `str`, append `.decode()`.
    If it feeds a bytes sink (file opened `"wb"`, a socket, a hash), leave as
    bytes.
  - Translate keyword arguments: `indent=2` → `option=orjson.OPT_INDENT_2`;
    `sort_keys=True` → `option=orjson.OPT_SORT_KEYS` (OR-combine multiple
    options).  Drop kwargs orjson cannot express and note them in residue.
  - **The output bytes change even with no kwargs to translate.** Two stdlib
    defaults have no orjson equivalent: `json.dumps` separates with `", "` and
    `": "` while orjson is always compact, and `ensure_ascii=True` escapes
    non-ASCII as `\uXXXX` while orjson always writes UTF-8.  So only a call
    already passing `separators=(",", ":")` **and** `ensure_ascii=False`
    round-trips byte-identically.  For every other `dumps`, find what consumes
    the string.  If anything hashes it, commits it, diffs it, signs it or
    compares it byte-for-byte, prove the change on real input (for a
    committed file, dump its current content both ways and compare) and say
    in residue what will change.  Do not rewrite a committed file to match;
    the edit touches the call site only.
  - **A `default=` callable survives the swap but STOPS BEING CALLED for the
    types orjson serializes natively** — `datetime`, `date`, `time`, `uuid.UUID`,
    and dataclasses.  NumPy is **not** native unless `orjson.OPT_SERIALIZE_NUMPY`
    is set; without that option, `default=` still runs for ndarray values.
    `json.dumps` has no native support for any of them, so a `default=` on a
    stdlib call is very often there precisely to encode one, and orjson silently
    takes its own path instead.  This is the single highest-risk edit in this
    rule: the finding clears, the tests that do not assert on the encoded value
    pass, and the output shape changes.
    Before swapping any `dumps` that passes `default=`, read the callable.  If
    it handles a natively-serialized type, add the matching passthrough option
    so orjson routes that type back to it — `orjson.OPT_PASSTHROUGH_DATETIME`
    for `datetime`/`date`/`time`, `orjson.OPT_PASSTHROUGH_DATACLASS` for
    dataclasses — and OR it with any other option.  UUID is native and has **no**
    passthrough: if the callable handles UUID, the swap is NOT mechanical —
    leave the site on stdlib `json` and residue it.  The same residue path
    applies to any other natively-serialized type with no passthrough.
    Do not add `OPT_SERIALIZE_NUMPY` as part of this swap: that would start
    natively encoding arrays the callable currently handles.
    Seen in the field on a connector whose `default=` mapped `datetime` to
    epoch-milliseconds because the publish app rejects ISO strings with
    `ATLAS-404-00-007 invalid value for type date`.  The straight swap reverted
    every date attribute to ISO; conformance reported a clean fix and only a
    pre-existing unit test caught it.
  - Ensure `import orjson` is present at module top (it is a core SDK
    dependency); add it if missing.
  - **Three stdlib tolerances orjson drops — check each before swapping a
    `dumps`:**
    - *Non-`str` dict keys.*  `json.dumps({1: "a"})` coerces the key to `"1"`;
      `orjson.dumps` raises `TypeError`.  Add `orjson.OPT_NON_STR_KEYS` unless
      every dict the call sees is provably string-keyed.  A `default=` callable
      does not help: it is never consulted for keys.
    - *NaN / Infinity.*  `json.dumps(float("nan"))` writes the non-standard
      `NaN` token; `orjson.dumps` writes `null`, and `orjson.loads` rejects the
      `NaN` token.  Usually an improvement (downstream JSON parsers reject
      `NaN` too), but data written by the old code and read back by the new
      one will now fail to parse where it holds that token — say so, and make
      sure the reader's fallback covers it.
    - *Integers beyond 64 bits.*  stdlib encodes any `int`; `orjson.dumps`
      raises `JSONEncodeError` above 2**64 (`OPT_STRICT_INTEGER` lowers the
      limit to 2**53).  Confirm the field's range before swapping.
  - The swap also changes whitespace (`{"x":2}`, not `{"x": 2}`).  A test that
    pins the exact encoded string must be updated to the new output — that is
    the output genuinely changing, not gaming the gate — and a round-trip test
    is the stronger assertion to add beside it.
  - `orjson.JSONDecodeError` subclasses `json.JSONDecodeError` and
    `ValueError`, so an existing `except json.JSONDecodeError` still catches;
    switch the name to `orjson.JSONDecodeError` when `import json` goes away.

  The orthogonal gate **bites** here for type errors: a `bytes`/`str`
  regression on any covered path fails the behavioural tests, so a careless
  swap is caught by `orthogonal-gate` before the edit survives.  It does
  **not** bite on byte-level output changes, which parse to the same value
  and pass any test that compares parsed JSON; the separators/`ensure_ascii`
  bullet above is the only check for those.  Classification is always
  `"judgment"` (the decode/kwargs call requires reading the call site), so the
  edit is also routed to residue for human confirmation.

- **O002 LegacyAssetSerialization** (asset-mapper, BLDX-1492) — an asset is
  serialized with the pydantic `.dict()` method in a module that imports pyatlan
  asset models.  The asset-mapper transform task writes assets through the SDK's
  serialization seam — `out_f.write(entity_bytes(asset, envelope=...) + b"\n")`
  (`from application_sdk.common.asset_serialization import entity_bytes`) — which
  emits the nested-entity wire shape the platform ingests; `.dict()` produces a
  flat dict that still needs hand-conversion.  For a `pyatlan_v9` asset, draft
  the switch to `entity_bytes(asset, ...)` (note it returns `bytes`, so the sink
  must be a bytes/JSONL writer).  Never draft `asset.to_nested_bytes()`: it
  bypasses the seam and trips P052.

  If the asset is a legacy `pyatlan.model.assets` model (O004 fires in the same
  file, and the O002 message says so), **do not** draft the `entity_bytes`
  switch on its own: a v1 model falls through to `model_dump()`, whose
  snake_case field names are not the Atlas wire shape, so the rewrite would
  emit malformed entities while looking finished.  The fix is the O004
  migration first — move the model to `pyatlan_v9.model.assets` and confirm
  every field the mapper sets exists on the v9 class — and only then the
  serialization switch.  Draft both together as one proposal, or route the
  site to residue if the model migration is not in reach.

  If the flagged `.dict()` is on a **non-asset** pydantic model, propose an
  inline `# conformance: ignore[O002] <reason>` instead.

- **O003 UntypedAssetMapperReturn** (asset-mapper, BLDX-1492) — a function builds
  a pyatlan asset and returns it but declares no return annotation.  Draft the
  smallest mechanical fix: add `-> <Asset>` naming the asset class the function
  constructs (e.g. a function building and returning a `Table` becomes
  `def map_table(...) -> Table:`).  If the function legitimately returns a union
  or `Optional`, annotate accordingly.  Classification is `"judgment"` only
  because the author may intend a wider return type; the edit is otherwise
  mechanical.

- **O004 LegacyPyatlanAssetImport** (asset-mapper, BLDX-1492) — app code imports
  asset models from the legacy `pyatlan.model.assets` package instead of
  `pyatlan_v9.model.assets` (the optimized v9 surface the asset-mapper pattern is
  built on).  `pyatlan_v9` ships inside the existing `pyatlan>=9` dependency — no
  dependency change is needed.  Draft a proposal in two parts, and **never** a
  blind `pyatlan` → `pyatlan_v9` string swap:

  1. **Rewrite the import** — `from pyatlan.model.assets import Table, Column` →
     `from pyatlan_v9.model.assets import Table, Column`.

  2. **Adapt every construction site** — the v9 models are not a drop-in rename:
     attribute names and the serialization API differ.  In particular, switch
     asset serialization from the pydantic `asset.dict()` form to the SDK's
     `entity_bytes(asset, ...)` seam used by the transform task (this also clears
     any O002 finding without tripping P052).  Read each `Table(...)`/`Column(...)` call and confirm the
     kwargs exist on the v9 model; note any that don't in residue rather than
     dropping them.

  Shape the result after the reference asset-mapper apps (`atlan-openapi-app`,
  the migrated `atlan-metabase-app`); full guidance in `docs/upgrade-guide-v3.md`.
  **Intentional legacy pin:** if the connector is deliberately still on the
  built-in `AtlasTransformer` (which depends on `pyatlan`), propose an inline
  `# conformance: ignore[O004] <reason>` naming that constraint instead — the
  B001 deprecation nudge will steer the larger migration.  Classification is
  `"judgment"`.

- **O006 DirectRocksdictImport** (canonical-dependency) — app code imports the
  `rocksdict` package directly (`from rocksdict import Rdict`, `import
  rocksdict`, or an aliased/submodule form) and hand-rolls its own RocksDB
  wrapper.  The SDK already ships
  `application_sdk.common.spillable_dict.SpillableDict` — a
  `MutableMapping`-compatible dict built on the same `rocksdict.Rdict` — so the
  finding is a nudge to stop reinventing the wrapper.  `SpillableDict` is **not**
  a drop-in import swap, so this is never mechanical — judge the site first:

  - **Values** — `SpillableDict` pickles values on write and unpickles on read,
    so a hand-rolled JSON serialize/deserialize step (the shape that bit
    CNCT-80/CNCT-191: `put()` special-cases `str`, `get()` unconditionally runs
    `json.loads()`, and a stored string that is also valid bare JSON round-trips
    as `int`/`bool`/`None`) is simply deleted, not translated.  If the wrapper's
    only serialization is that hand-rolled JSON step, migrating to
    `SpillableDict` removes the bug class outright.
  - **Keys** — `SpillableDict` restricts keys to `str | int | float | bool |
    bytes` and raises `TypeError` on anything else.  If the flagged wrapper keys
    on a tuple, a `None`, or a custom object, a plain migration will not type —
    either reshape the key into a supported primitive, or suppress with
    `# conformance: ignore[O006] <reason naming the non-primitive key type>`
    (routed to residue).
  - **Options surface** — `SpillableDict` builds its own `rocksdict.Options`
    internally and exposes no tuning surface.  A wrapper that passes a custom
    `Options`/`BlockBasedOptions` (block cache, compaction style, prefix
    extractors) has no equivalent knob — that is a deliberate-`Options` case,
    so propose an inline `# conformance: ignore[O006] <reason>` naming the
    tuning it depends on rather than migrating.
  - **Association-list / merge semantics** — `SpillableDict.append_to_key` is a
    read-modify-write list append (unpickle the whole list, append, repickle),
    **not** RocksDB's atomic merge operator: it is not thread-safe and costs
    O(K²) for K repeated appends to one key.  If the flagged wrapper relies on
    a native `Rdict` merge/`append_to_key` for atomicity or for high-frequency
    append throughput, that is a justified suppression —
    `# conformance: ignore[O006] <reason>` naming the merge semantics
    `SpillableDict` does not provide.

  When none of those carve-outs applies (picklable values, keys already in
  `str | int | float | bool | bytes`, no custom `Options`), draft the migration:
  replace the `rocksdict` import and the hand-rolled wrapper class with
  `from application_sdk.common.spillable_dict import SpillableDict`, route the
  call sites through the `MutableMapping` API, and drop the now-dead
  serialize/deserialize helpers.  Classification is `"judgment"` (the
  key-type / `Options` / merge-semantics call requires reading the call site),
  so the edit is routed to residue for human confirmation.
- **O005 UnresolvedAppNamePlaceholder** (dag-write-path, CONNECT-183) — a plain
  string literal (or an escaped-brace f-string, `f"atlan-{{app_name}}-prod"`,
  whose braces are *not* interpolated) still carries an unsubstituted
  `{app_name}` token, so the literal token freezes into whatever it is assigned
  to instead of the real app name.  This is never a mechanical fix — the right
  resolution depends on where `app_name` is actually available, so
  classification is always `"judgment"`:
  - If the app name is already in scope at the literal's site (a variable, a
    parameter, a manifest field), draft the smallest resolution: an f-string
    (`f"atlan-{app_name}-prod"`) or `"atlan-{app_name}-prod".format(app_name=...)`.
    Keep every other placeholder in the template untouched — a second
    unresolved token (e.g. `{dep}`) is out of O005's scope and must not be
    "fixed" by binding it to something wrong.
  - If the name is *not* in scope, the value must be threaded in from the
    caller that has it — draft that threading, or note in residue that the
    call graph needs a human (the rule is WARN-tier precisely because this
    judgment cannot be automated).
  - **Shared helper (FND-195):** `application_sdk.common.task_queue`
    (`derive_task_queue` / `resolve_manifest_tokens`) is the canonical
    remediation target, but it ships only with the SDK release that carries
    FND-195 — do **not** draft an import of it into a repo pinned to an
    earlier SDK (the import would not exist).  Prefer the in-scope f-string /
    `.format(app_name=...)` fix there, and note the helper as the follow-up
    once the SDK is bumped.
  - **Legitimate cross-file resolution:** if the template is deliberately
    resolved by a caller in a *different* file than the one scanned (the
    rule's known blind spot), propose an inline
    `# conformance: ignore[O005] <reason naming the resolving caller>` instead
    of an edit, and route it to residue for human audit — an escaped-brace
    f-string (`f"{{app_name}}"`) is almost never legitimate, since nothing
    downstream can interpolate it either.
  - Never "resolve" the token by hardcoding a concrete app name: that hides
    the finding while freezing the wrong value into every other tenant.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and the site legitimately needs stdlib `json` (e.g.
interop with a library that requires a `str` and the bytes-decode round-trip
is wasteful, or a `json.JSONEncoder` subclass), the model may propose an
inline `# conformance: ignore[O001] <justification>` instead of a fix.  Route
every suppression to residue for human audit.
