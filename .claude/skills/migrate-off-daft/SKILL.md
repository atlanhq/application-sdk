---
name: migrate-off-daft
description: >
  Migrate a connector app across the SDK 3.20.0 daft cliff. In v3.20.0
  (commit 41f32e1d, PR #2300) the SDK removed daft entirely and rerouted the
  transformation layer to DuckDB + pyarrow + orjson, in one cut — any app
  whose lockfile crosses 3.19.x → 3.20.0+ hits every break at once. The skill
  resolves the latest published SDK version (never a hardcoded floor),
  detects every daft dependency the app has — declared, transitive, or
  behavioral — and classifies each site as wrapper vs compute against the
  known breakage classes: empty [daft] extra (and override-dependencies
  pins that silently stop protecting), changed transform_metadata
  input/output contracts (direct or via SDK internals like
  prepare_template_and_attributes), DuckDB engine without its extra, the
  SDK's own arrow-conversion call that only works on duckdb >=1.5.0 (broken
  on every release 3.20.0-3.25.0), literal-vs-column precedence flip,
  first-record schema inference, test coupling — plus the heavy-user classes: python-object UDF columns with no
  DuckDB equivalent, daft.sql caller-frame binding vs explicit register,
  daft-planner-specific type coercions, daft-safety concurrency
  architecture to retire deliberately, and live-daft golden oracles that
  must be frozen to static fixtures before daft is removed. Drives the
  fixes with the developer. Boundary policy: an app may keep daft for its
  own compute (declared as a direct dependency, never via the SDK's empty
  [daft] extra), but no daft object may cross an SDK boundary — every
  SDK-facing usage is migrated safely (convert at the boundary or delete
  the wrapper). Default fix stance for wrapper sites: delete the daft
  wrapper and pass list[dict], guarded against schema loss. The done-bar
  is output parity: the
  migrated app's transformed output must be structurally identical to the
  pre-3.20 output with no attribute lost — any observable difference is a
  defect unless the developer explicitly accepts it. Run this BEFORE any
  skill that bumps the SDK (adopt-preflight-gate, upgrade-v3) when the app
  is below 3.20.0.
mandatory_triggers:
  - "/migrate-off-daft"
  - "migrate off daft"
  - "daft migration"
  - "app breaks on daft after SDK bump"
optional_triggers:
  - "remove daft from this app"
  - "duckdb pyarrow migration"
  - "SDK bump broke transformers"
owner: connector-platform-team
last_updated: "2026-07-30"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - pyproject.toml (daft extra replaced with the extras the app actually needs — [sql] for QueryBasedTransformer users; daft declared directly only in the documented last-resort case)
  - uv.lock (resynced; verified to contain duckdb when the app transforms, and daft only if deliberately kept)
  - app code with the daft wrapper deleted (from_pylist/count_rows/to_pylist/iter_rows call sites) or bridged
  - extractor/template fixes for literal-name collisions (the silent-corruption class)
  - updated tests, including a heterogeneous-records regression test where list[dict] is passed
---

# Migrate off daft (the SDK 3.20.0 cliff)

## The cliff, precisely

One commit, one release: `41f32e1d` — *remove daft entirely, replace with
pyarrow/orjson/duckdb* (#2300) — shipped in **v3.20.0** (2026-06-25). Nothing
was staged in 3.19.x or 3.21.x, so an app crossing 3.19 → 3.20+ hits every
change below simultaneously. Judge exposure from **`uv.lock`** (the resolved
version), not the declared floor — floors are usually loose, and an app whose
lock already resolves ≥3.20 is *already broken* regardless of what branch you
are on; reverting a later bump fixes nothing.

## Step 0 — Resolve the real target version (never hardcode)

```bash
curl -s https://pypi.org/pypi/atlan-application-sdk/json | jq -r .info.version
```

Then apply the org release-age cooldown: default to the newest version
public ≥7 days (`jq -r '.releases[<ver>][0].upload_time'` gives the date).
A version inside the cooldown window is only adoptable for a genuine security
fix; otherwise pin the newest version outside the window and note the newer
one for follow-up. Other skills' hardcoded floors (e.g. adopt-preflight-gate's
`>=3.24.1`) are *minimums for their feature*, not the target — pin the
resolved latest that satisfies both.

## Step 1 — Detect every daft dependency (declared, transitive, behavioral)

Run all of these; each maps to a breakage class in step 2. Report the full
hit list to the developer before editing anything.

```bash
# (a) module-scope imports — fail at import/collection time once daft vanishes
grep -rn "^import daft\|^from daft\|    import daft\|    from daft" app/ tests/

# (b) the empty-extra trap — pyproject asks for [daft], lock has no daft
grep -n "atlan-application-sdk\[" pyproject.toml
grep -c 'name = "daft"' uv.lock   # 0 on any lock resolving SDK >=3.20

# (c) deleted SDK surface
grep -rn "_execute_query_daft" app/ tests/

# (d) daft methods on transformer results — runtime AttributeError
grep -rn "\.to_pylist()\|\.to_pydict()\|\.iter_rows()\|\.count_rows()\|\.to_arrow()" app/

# (e) the deprecated enum (silently routes to pandas since 3.20)
grep -rn "DataframeType.daft" app/ tests/

# (f) dead config and dead error-code matching
grep -rn "DAFT_" Dockerfile* helm/ .env* 2>/dev/null
grep -rn "_DAFT_" app/ tests/

# (g) engine dependency — QueryBasedTransformer now needs duckdb
grep -rn "QueryBasedTransformer\|AtlasTransformer" app/
# duckdb ships only in the SDK's [sql] and [incremental] extras — check yours

# (h) literal-collision candidates (the silent-corruption class, step 2 #5)
grep -rn "source_query: *\"'" app/transformers/*.yaml app/**/*.yaml 2>/dev/null

# (i) pins that LOOK protective but aren't — uv override/constraint entries
#     only constrain packages already in the graph; once the [daft] extra
#     empties, an override-dependencies daft pin is a silent no-op
grep -n "override-dependencies\|constraint-dependencies" pyproject.toml

# (j) indirect SDK-contract exposure — apps that OVERRIDE transform_metadata
#     but still call SDK internals; grep the helpers, not just the entrypoint
grep -rn "prepare_template_and_attributes\|generate_sql_query\|get_grouped_dataframe_by_prefix" app/

# (k) daft as a COMPUTE ENGINE, not a wrapper (step 2b classes)
grep -rn "daft.sql(\|\.apply(\|DataType.python()\|daft.col(\|daft.lit(\|from_arrow(" app/

# (l) daft-specific infrastructure: executors, exception classifiers,
#     metrics, review rules — orphaned (not just dead) when daft goes
grep -rn "DAFT_EXECUTOR\|DaftCoreException\|daft_executor\|_classify_daft" app/ tests/ REVIEW.md .claude/ 2>/dev/null

# (m) parity/golden tests that compute their reference BY RUNNING daft live
grep -rln "daft" tests/ | xargs grep -ln "golden\|parity\|_golden_records" 2>/dev/null

# (n) class 3b — the SDK's own arrow conversion needs duckdb >=1.5.0. Read both
#     resolved versions from the lock: SDK 3.20.0-3.25.0 + duckdb <1.5.0 = the
#     transform cannot execute at all, whatever the app does.
grep -A1 '^name = "atlan-application-sdk"$' uv.lock
grep -A1 '^name = "duckdb"$' uv.lock
```

For (h), extract each literal column *name* (e.g. `status: source_query:
"'ACTIVE'"` → `status`) and intersect it with the keys the app's extractors
actually emit for that typename. Any intersection is a live corruption site
on SDKs predating the row-local precedence fix, and a design smell to review
with the developer after it (see class 5 for the version split).
The membership check is **case-sensitive Python** (`in
dataframe.schema.names`) while DuckDB resolves identifiers
case-insensitively — a same-name-different-case pair is a latent trap even
when today's casing is benign; note it in the report.

Then **trace reachability**: for every module-scope `import daft`, walk the
import chain from `main.py` / the App module. A daft import reachable from
the entrypoint chain is a **worker-boot crash for every workflow** —
including ones (e.g. a miner) that use no daft at all. Blast radius is the
app, not the transform.

Finally, **classify each daft call site as wrapper vs compute** — this
drives the whole fix stance. Wrapper = the frame exists only to satisfy an
SDK signature (list[dict] in, list[dict] out) → step 3's default deletion
applies. Compute = daft does work of its own (UDFs, expressions, casts
driven by daft's planner, its SQL entrypoint) → the app may keep daft for
those sites (declared directly — see step 3's boundary policy) or replace
them per step 2c; either way each site needs a decision with parity
evidence, and no daft object may cross an SDK boundary afterwards.

## Step 2 — The core breakage classes (1–7, plus 1b/2b/3b; heavy-user classes 8–12 are in step 2c)

### 1. `[daft]` extra is declared but empty — daft silently leaves the lock

Since 3.20 the SDK still `Provides-Extra: daft` with **zero** matching
`Requires-Dist` entries (`pyproject.toml:73-75` documents it as a no-op alias,
removed next major). An empty extra is not an error in uv — no warning, no
resolution failure; daft just stops arriving while `pyproject.toml` still
*looks* like it requests it. Module-scope `import daft` then fails at import
time: the worker can't load the app module and pytest fails at **collection**,
not at runtime.

Lesson to state to the developer: **if you import it, declare it** — an extra
on someone else's package is not a supply contract you control.

Fix: don't reinstall daft to quiet the import (see class 2 for why). Delete
the import with the wrapper (step 3). Replace `[daft,...]` in the app's
extras with what it actually needs — `[sql]` supplies `duckdb` +
`duckdb-engine` for transformer users.

### 2. `transform_metadata` input contract: daft DataFrame → `pa.Table | pd.DataFrame | list[dict]`

The coercion (the input-normalization branch at the top of
`QueryBasedTransformer.transform_metadata`)
converts lists and pandas frames; a daft DataFrame matches **neither branch
and passes through unconverted** — there is no `else: raise`. It then dies
inside `db.connection.register("dataframe", <daft df>)`, deep in DuckDB,
reading like a data/schema problem rather than a version mismatch. That is
why reinstalling daft makes diagnosis *worse*, not better: it moves a clean
`ModuleNotFoundError` to an inscrutable engine error.

Known misleading signature: `AttributeError: 'function' object has no
attribute 'names'` = a daft DataFrame reached code expecting pyarrow.
`pa.Table.schema` is a property; `daft.DataFrame.schema` is a **method**, so
`.schema.names` asks a bound method for `.names`. The message never says
"wrong dataframe type" — that is what happened.

### 3. SQL engine is DuckDB now — and `duckdb` may not be installed

3.19 executed the generated YAML-template SQL on daft-SQL (which bound the
`FROM dataframe` identifier by *inspecting the caller's local variables*).
3.20+ registers the table explicitly and executes on DuckDB
(the `DuckDBConnectionManager` block in `transform_metadata`). The
`duckdb` import is **function-local**, so
nothing fails at boot or import — the first real transform call raises
`ModuleNotFoundError: duckdb`. Ships only in the SDK's `[sql]` and
`[incremental]` extras.

### 3b. The SDK's own arrow conversion only works on duckdb >=1.5.0 — an SDK bug

With classes 2 and 3 fixed, the transform can still die *inside the SDK*:

```
AttributeError: '_duckdb.DuckDBPyConnection' object has no attribute
'to_arrow_table'. Did you mean: 'fetch_arrow_table'?
```

`transform_metadata` called `db.connection.execute(sql).to_arrow_table()`.
`conn.execute()` returns the **connection**, not a relation, and
`to_arrow_table` was added to `DuckDBPyConnection` only in **duckdb 1.5.0** —
while the SDK's `[sql]` / `[incremental]` extras allow
`duckdb>=1.1.3,<1.6.0`. Measured across the window: absent on 1.1.3, 1.3.2,
1.4.4; present on 1.5.0+. So the YAML transform path is unusable on **every
SDK release 3.20.0 through 3.25.0** for any app whose lock resolves duckdb
below 1.5.0, and works only by luck above it. Fixed by PR #2940
(`db.connection.sql(sql).to_arrow_table()` — a relation, where the method
predates the whole range); unreleased as of 2026-07-30, so check the app's
pinned SDK rather than assuming.

Route around it, in order of preference:

- **Pin duckdb up** — declare `duckdb>=1.5.0,<1.6.0` as a direct app
  dependency. uv intersects it with the SDK's range, the method exists, and no
  SDK code is touched. Delete the pin once the app is on the release carrying
  #2940.
- **Override `transform_metadata`** app-side with the one-line fix and a TODO
  pointing at #2940. Heavier, and it re-exposes the app to class 2b.

Do not chase this as an app-side dataframe problem: the input coercion has
already succeeded by the time it fires, and the last app frame in the
traceback is just the `transform_metadata` call. Note also that
`execute(...).fetch_arrow_table()` — the other obvious repair — works at the
floor but is **deprecated as of duckdb 1.5.0**, so it trades the crash for a
`DeprecationWarning` on the versions most locks resolve.

### 4. `transform_metadata` return contract: daft DataFrame → `list[dict] | None`

Any `.to_pylist()` / `.to_pydict()` / `.iter_rows()` on the result raises
`AttributeError: 'list' object has no attribute ...`. Items are dicts shaped
`{"typeName": ..., "status": ..., "attributes": {...}}` — iterate them
directly. Note the classes mask each other in order 2 → 3 → 3b → 4: fixing one
surfaces the next; budget for the sequence, not one crash.

### 5. Literal-vs-source-column precedence flipped — SILENT corruption

The one that matters most, because nothing crashes. Mechanism (verified in
SDK code, not folklore):

- A quoted-literal template column (`status: source_query: "'ACTIVE'"`) is
  classified as a literal and its SQL expression is emitted as
  `"status" AS "status"` — a **self-reference to a dataframe column**, not
  the literal (`convert_to_sql_expression(is_literal=True)` via
  `get_sql_column_expressions`).
- The literal's value is moved into `default_attributes` (in
  `prepare_template_and_attributes`) and applied only where the table has no
  source data for that name.
- So when the extractor's records carry a genuine value under the same name,
  the source value wins and the template literal does not apply. Under
  daft-SQL the literal always won; the engine swap inverted it.

**How much wins depends on the SDK version** (same before/after split as
class 6):

- **Before the row-local fix** (3.20 through 3.24.x): the decision is
  column-level — if the colliding key is in the table's schema, the literal
  is discarded for the *whole batch*, including rows that had no value
  (published as null). On pre-key-union pins it is also record-order
  dependent. Symptom seen live: every published entity of one typename
  carrying the source system's lifecycle status instead of the reserved
  Atlan `ACTIVE`. Against these SDKs, **any** step-1(h) intersection is a
  live corruption site.
- **After the row-local fix** (first release carrying
  `test_literal_precedence_is_row_local_on_colliding_column`): a genuine
  source value wins for its own row, the literal fills the rows without
  one, and a type-incompatible collision raises a typed
  `IncompatibleDefaultTypeError` instead of corrupting. An intersection is
  then a *design smell* to review with the developer, not automatic
  corruption.

Either way, fix collisions at the **source**, not downstream: rename the
extractor key (`record["analysisStatus"] = record.pop("status", None)`) so
the reserved name is unambiguous. Run the step-1(h) intersection for
*every* template × extractor pair; blast radius is exactly the typenames
whose records carry a colliding key.

### 6. `pa.Table.from_pylist` infers schema from the FIRST record only

```python
pa.Table.from_pylist([{"a": 1}, {"a": 2, "b": 3}]).schema.names  # ['a'] — 'b' gone, no error
```

Any batch whose first record lacks an optional key drops that attribute for
the whole batch — incomplete entities published, no exception anywhere.
daft's `from_pylist` unified schemas across records, so apps never had to
care.

The SDK's own `list[dict]` coercion had this hazard until it was fixed to
build the table over the union of keys (the list branch of
`QueryBasedTransformer.transform_metadata`; test:
`tests/unit/transformers/query/test_sql_transformer.py::test_transform_metadata_list_input_unifies_keys_across_records`).
The same fix keeps class-5 precedence sane and row-local: a genuine source
value wins for its own row, the template literal fills the rows without one
(tests: `test_template_literal_wins_when_colliding_column_is_all_null`,
`test_literal_precedence_is_row_local_on_colliding_column`).
**Check the app's pinned SDK actually contains that fix** (the tests above,
or read the coercion) — on older 3.2x pins, passing heterogeneous
`list[dict]` is lossy and the app must guard: normalize keys itself or build
the `pa.Table` with an explicit schema. App-side `pa.Table.from_pylist`
calls the app writes are **always** the app's problem — same guard applies.

### 1b. A pin that looks protective and isn't

An app may carry `override-dependencies = ["daft==0.7.x"]` (or a constraint
entry) and treat it as a hard guardrail. uv overrides only constrain packages
**already in the dependency graph** — once the SDK's `[daft]` extra empties,
the override constrains nothing and daft leaves the lock anyway, with the
pyproject still reading like it's pinned. Only a **direct dependency** keeps
daft installed. Found live in atlan-snowflake-app (`override-dependencies`
believed to be load-bearing; it wasn't).

### 2b. Indirect contract exposure — overriding `transform_metadata` doesn't insulate you

An app that fully overrides `transform_metadata` and never calls the SDK's
still breaks if the override calls SDK **internals**:
`prepare_template_and_attributes` / `generate_sql_query` read
`dataframe.schema.names` (`get_sql_column_expressions`), `len(dataframe)` and
`.append_column` (the default-attributes loop) — none of which work on a
daft frame — and
`get_grouped_dataframe_by_prefix` now takes a `pa.Table` and returns
`list[dict]`, so the override's own return type changes out from under its
callers (class 4, one hop removed). Grep the helpers (step 1j), not just the
entrypoint.

### 7. Test coupling exposed by the fixes

Parity/legacy tests that feed one shared dict to both the v3 transformer and
a legacy renderer break the moment a fix mutates the record (e.g. the
class-5 `pop`). The coupling was always fragile — it worked only while
enrichment was purely additive. Fix by giving the legacy path its own copy of
the raw record, with a comment saying why. Similarly, regression fixtures
that exist to reproduce a *daft behavior* (e.g. daft's JSON reader producing
`numpy.ndarray`) should keep their guarantee but synthesize the input
directly (numpy) instead of via daft — never drop the assertion with the
dependency.

## Step 2c — Heavy users: when daft is a compute engine, not a wrapper

Classes 1–7 model daft as an SDK-signature wrapper. Heavy users (reference:
atlan-snowflake-app) also use it as an engine, and each of these needs a
designed replacement, not a deletion:

- **8. Python-object UDF columns have no DuckDB/pyarrow equivalent.**
  `daft.col(c).apply(fn, return_dtype=daft.DataType.python())` holds
  arbitrary Python objects (parsed JSON, lists of structs) mid-pipeline.
  DuckDB has no python-object column type and `pa.Table` cannot hold one.
  Relocate that logic to run **after** materialization to `list[dict]` —
  and treat the move as an output-parity hazard, because it changes where
  null/type coercion happens.
- **9. `daft.sql()` binds tables by caller-frame magic; DuckDB needs an
  explicit register.** `daft.sql("... FROM dataframe")` resolves the
  identifier by inspecting the caller's local variables. The DuckDB
  equivalent requires `conn.register("dataframe", table)` before execute —
  a mechanical rewrite that misses the registration dies with
  `Catalog Error: Table 'dataframe' does not exist`.
- **10. Daft-planner-specific type coercions become wrong-shaped.** Casts
  added to satisfy daft's plan-build-time type validation (Null→String,
  Decimal→Int64, etc.) may be unnecessary under DuckDB — or actively change
  results (e.g. a Null→String cast feeding a `CAST(col AS TIMESTAMP)` /
  `DATE_TRUNC`). Audit every schema-driven cast helper against the new
  engine's semantics instead of porting it verbatim; expect a per-dialect
  override map, not a shared one.
- **11. Concurrency architecture built for daft must be retired
  deliberately.** Single-thread executors, plan-cache-corruption defences,
  daft-exception classifiers, and their metrics/review rules exist because
  daft 0.7.x's plan cache was not concurrency-safe. DuckDB is natively
  multi-threaded: keeping the executor silently serializes the new engine
  (throughput cap); deleting it uncorks concurrency the app's memory budget
  was never sized for. Removing the guard and re-tuning the concurrency
  setting are one decision, taken with the developer.
- **12. Freeze golden fixtures BEFORE removing daft.** If parity tests
  compute their reference by *running the daft path live* at test time,
  deleting daft deletes the only proof the replacement engine matches.
  First run the daft path once, commit its output as static fixtures, and
  point the parity tests at them — then remove daft.

Housekeeping the audit should also flag: stale ARCHITECTURE/CLAUDE docs
describing daft code that no longer exists (they misdirect the migration),
obsolete pyproject comments reasoning about daft's pyarrow ceiling (delete,
don't adjust — verify the app's pyarrow window overlaps the SDK's
`>=23.0.1,<24` first), and the fact that `QueryBasedTransformer` itself is
deprecated for v4.0 — the daft migration and the asset-mapper migration are
the same code, so at minimum record the follow-up so it isn't done twice.

## Step 3 — The fix, in order

Default stance for **wrapper** sites (per the step-1 wrapper-vs-compute
classification): **delete daft, don't port it.** The records were
`list[dict]` on the way in and `list[dict]` on the way out — the daft
DataFrame existed only to satisfy the old signature. The migration is a
deletion. **Compute** sites follow step 2c instead, and for heavy users the
very first action — before any deletion — is freezing live-daft golden
fixtures (class 12), or the parity bar in step 4 becomes unprovable:

```python
# before
df = daft.from_pylist(records)
if df.count_rows() > 0:
    transformed = transformer.transform_metadata(dataframe=df, ...)
    rows = transformed.to_pylist()

# after
rows = transformer.transform_metadata(dataframe=records, ...) or []
```

1. Delete `import daft`, `daft.from_pylist`, `count_rows()` (usually
   redundant — records are already guarded non-empty upstream),
   `to_pylist()`.
2. `pyproject.toml`: `[daft,...]` → the extras actually needed (`[sql]` for
   transformer users; keep `[pandas]`/`[workflows]` etc. as-is). Then
   `uv lock --exclude-newer "$(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)"` (GNU
   date: `date -u -d '7 days ago' ...`) + `uv sync --all-extras
   --all-groups` — the extras change resolves fresh packages (duckdb and
   friends), and the bound keeps them inside the release cooldown. Then
   **verify the lock**:
   `grep -c 'name = "duckdb"' uv.lock` ≥1, `grep -c 'name = "daft"' uv.lock`
   = 0.
3. Apply the class-6 guard if the pinned SDK predates the key-union fix, or
   if the app builds pyarrow tables itself. Same check for class 3b: if the
   pinned SDK predates PR #2940, add the `duckdb>=1.5.0,<1.6.0` direct pin —
   and verify the lock actually moved (`grep -A1 '^name = "duckdb"$' uv.lock`).
4. Fix every class-5 collision found in step 1(h), at the extractor.
5. Update tests (class 7), and add the heterogeneous-records regression test
   for any `list[dict]` handoff.

**Keeping daft for the app's own compute is allowed — the boundary is the
SDK.** The policy, stated plainly:

- An app that genuinely uses daft as its own engine (UDFs, expressions, its
  SQL — the step-2c classes) **may keep it**. It must declare
  `daft>=0.7.15,<0.8` as a **direct dependency** — never via the SDK's
  `[daft]` extra (empty) and never via an `override-dependencies` pin
  (silent no-op, class 1b). If you import it, you declare it.
- What is **not allowed to survive the bump** is daft crossing an SDK
  boundary: a daft frame passed to `transform_metadata`, to SDK internals
  (`prepare_template_and_attributes`, `get_grouped_dataframe_by_prefix`),
  or to SDK readers/writers. Every such crossing must be migrated — either
  convert at the boundary (`dataframe.to_arrow()` going in, and consume the
  `list[dict]` return coming out) or take the default deletion where the
  frame was only a wrapper. The SDK will never see or supply daft again.
- Whichever path a kept-daft app takes, the step-4 output-parity bar and
  the class-12 golden-freeze rule apply unchanged, and the PR records which
  sites kept daft and why. Note the standing debt regardless: the
  transformer path is deprecated for v4.0 (asset-mapper is the target), so
  kept-daft compute will be revisited then.

## Step 4 — Validate: the done-bar is OUTPUT PARITY

The goal of this migration is that the app's transformed output is **the
same** as it was on the pre-3.20 SDK — same entity structure, same
attributes, same values for everything that matters. The engine swap is an
implementation detail; any observable output change (a dropped attribute, a
flipped literal, a reshaped record) is a defect, not an acceptable delta.
The only allowed differences are ones the developer has explicitly reviewed
and accepted (e.g. a class-5 fix that *corrects* a value the old engine got
right by accident — state those in the PR).

- **Parity check first-class**: transform a representative extract on the
  migrated code and diff it against golden output from the pre-migration
  branch (or the app's existing parity suite). Diff per typename; every
  difference must be explained.
- Full test suite; collection errors are class 1, `AttributeError`s are
  classes 2/4, `ModuleNotFoundError: duckdb` is class 3, and
  `DuckDBPyConnection has no attribute 'to_arrow_table'` is class 3b (an SDK
  bug — check the pinned SDK before suspecting the app).
- Boot the worker (`uv run main.py`) — class 3 hides from boot, so also run
  one real transform (or the parity suite) end-to-end.
- Parity tests against golden output are the only net that catches class 5 —
  if the app has none, at minimum assert the literal-declared attributes
  (`status == "ACTIVE"` etc.) on transformed output for every typename whose
  records carry a colliding key.
- The deployed image gets exactly `uv.lock` (`uv sync --locked`), so a wrong
  lock is a production outage, not a dev inconvenience — treat lock
  verification as part of done.

## Debugging discipline (from a real post-mortem)

Change **one variable at a time**. The observed wrong turn: installing an
unpinned daft while also on a new SDK, then attributing the failure to daft
API drift. Killing the hypothesis took re-running the identical venv against
the old SDK and the exact old daft pin. When a bump breaks an app, hold the
app's other deps fixed at what main resolves and vary only the SDK — the
lockfile diff between main and the branch is the complete list of what
actually changed.

Corollary from class 3b: **a declared version range is not a tested range.**
The SDK shipped that call in six releases because its own lock resolved the
newest duckdb in the window, where the method happens to exist — CI only ever
exercises the ceiling. When a dependency's declared floor sits far below what
the lock resolves, treat the gap as untested rather than supported — on the
app's own dependencies too.

## Agent protocol

Two stops, developer decides at each:

1. **After step 1** — the full detection report: every hit, its breakage
   class, the literal-collision intersections, and the proposed fix stance
   (default vs last-resort, with reasoning). No edits yet.
2. **After step 4** — evidence: suite output, lock verification, worker
   boot, and the class-5 assertion results. Then hand off for PR review.

If the app is also below the preflight-gate floor, run this skill **first**,
land it (or stack it), then `/adopt-preflight-gate` — its SDK bump assumes
the daft cliff is already behind you.
