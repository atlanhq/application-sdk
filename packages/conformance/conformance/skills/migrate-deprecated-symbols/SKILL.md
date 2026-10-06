---
name: migrate-deprecated-symbols
description: >
  Move a connector app off deprecated SDK symbols (B001) and off private
  modules and names it does not own (B008). B001 reads the deprecation
  manifest the SDK ships (generated from the @deprecated markers in SDK
  source), so the list of what to migrate is never hard-coded here: the
  finding message carries the SDK's own migration notice. The skill
  inventories every site, sorts each into a straight swap (same behaviour,
  new name or import path), a structural migration that belongs to another
  skill (storage readers and writers, transformers, metadata extractors), a
  behaviour change that needs the developer (PreflightStatus.PARTIAL,
  upload_to_atlan), or a private name with no public equivalent (justified
  ignore with a tracked id). It applies the swaps, routes the structural
  sites, and proves the result with re-detection and the test suite. Run it
  after any skill that raises the SDK, because a newer SDK can deprecate more
  symbols.
mandatory_triggers:
  - "/migrate-deprecated-symbols"
  - "deprecated SDK symbol"
  - "B001 deprecated"
  - "B008 private import"
optional_triggers:
  - "remove private SDK imports"
  - "fix deprecation warnings from application_sdk"
owner: connector-platform-team
last_updated: "2026-10-06"
staleness_days: 90
inputs:
  - app_root: "auto-detected — the directory containing app/ and pyproject.toml"
outputs:
  - app code and tests importing public SDK and third-party names only
  - app code using the replacement each deprecation notice names, for the straight swaps
  - justified ignores for private names with no public equivalent, each naming a tracking id
  - a routing list of structural sites handed to migrate-storage, migrate-asset-modeling or upgrade-v3
---

# Migrate deprecated and private symbols (B001, B008)

## Conformance rules this skill clears

B001, B008. These rules name this skill as their `remediation_reference`, and
`/remediate` hands their findings here. When the skill is done, run
`atlan-application-sdk-conformance detect --rule B001,B008` and confirm none
of these rule ids is still reported.

## Why this matters

A deprecated symbol is on a removal path: the next SDK major (or the version
in its notice) removes it, and the app breaks on that bump. A private name
(leading underscore) has no compatibility promise at all: SDK 3.36.0 reshaped
one private preflight module and 15 repos stopped collecting tests.

## What each rule fires on

| Rule | Fires on | Stops when |
|---|---|---|
| B001 | a symbol in the SDK's deprecation manifest: `from application_sdk.x import Name`, a subclass of it, a call `x.<method>()`, or an enum member such as `PreflightStatus.PARTIAL` | the site uses the replacement the notice names, or carries a justified ignore |
| B008 | an import of a `_`-prefixed module or name from a package the app does not own (SDK or third-party), or a reach-through `alias._private.x` | the import names a public module and name, or carries a justified ignore |

B001 matches method calls by name only (`x.upload_to_atlan(...)`), so an
unrelated object with the same method name can fire it; that is a false
positive, ignore it with that reason. B008 never fires on relative imports,
on names that exist in the repo, or on dunders.

`detect` does not scan `tests/`, but test code breaks on the same SDK
changes. Find those sites yourself and add them to the inventory:
`rg -n 'application_sdk\S*\._|PreflightStatus\.PARTIAL' tests/` plus every
deprecated symbol the app-code findings named.

Suppression form, on the line or on a comment-only line directly above:
`# conformance: ignore[<ID>] <reason>`.

The deprecation manifest ships in the package:
`$(dirname "$(atlan-application-sdk-conformance programs-dir)")/data/deprecated_symbols.json`.
Each entry has `symbol`, `kind`, `module`, `message` (the migration notice)
and `removal_version`. The replacement exists only in `message`; read it, never
guess it. Two cautions: a notice can cite a doc that is only in the
`atlanhq/application-sdk` repo (for example `docs/agents/coding-standards.md`),
so read it on GitHub; and a notice that points at a private module
(`application_sdk._runtime...`) would create a B008 — treat that site as
**no public equivalent** instead of following it.

## Step 0 — Preconditions

1. Run the skills listed before this one in
   `$(atlan-application-sdk-conformance skills-dir)/order.txt` first, so the
   manifest matches the SDK the app will ship with.
2. Record the baseline outside the repo:
   `atlan-application-sdk-conformance detect --rule B001,B008 --output "$TMPDIR/before.sarif"`.
   Run the tests with every group and extra installed
   (`uv sync --all-groups --all-extras && uv run pytest tests/unit tests/integration`).
   Record the tests that already fail; they do not block this skill, and they
   must not get worse.

## Step 1 — Inventory and classify every site

One row per site: file:line, the symbol, the notice, and one class. B001
flags the **import**; list every use of the imported name too (one row per
use), because each use changes with the migration.

- **swap** — the replacement has the same behaviour and signature, or the
  public path exports the same object. Known examples:
  - `DataframeType.daft` → `DataframeType.pandas`.
  - `SqlApp._resolve_credential_ref` → `SqlApp.resolve_credential_ref`.
  - `application_sdk.execution._temporal.activity_utils.get_object_store_prefix`
    → `from application_sdk.execution import get_object_store_prefix` (the
    same function, exported publicly). Replacing it with FileReference outputs
    is a storage migration, not part of this swap.
  - Third-party privates with a public home, for example
    `confluent_kafka.admin._metadata` → `confluent_kafka.admin`, or
    `ipaddress._BaseAddress` in an annotation →
    `ipaddress.IPv4Address | ipaddress.IPv6Address`.
  - `storage.ops._resolve_store(None)` used only to ask "is an object store
    configured?" → `infra = get_infrastructure()` (from
    `application_sdk.infrastructure.context`), then
    `configured = infra is not None and infra.storage is not None`. It
    returns `None` when no context is set, so check that first; it replaces
    the try/except around the old call.
  Confirm each one: open the replacement in the installed SDK and check the
  signature before you call it a swap.
- **behaviour** — the replacement changes what the app does. Developer
  decision, per site:
  - `PreflightStatus.PARTIAL` → `READY` keeps the gate's behaviour (the gate
    already treats PARTIAL as READY); `NOT_READY` blocks the run. Either way
    the status label in the UI, Pulse and the Automation Engine event changes.
    Change the tests that assert PARTIAL and any message map keyed on it in
    the same step.
  - `upload_to_atlan(...)` → `App.upload(UploadInput(local_path=..., tier=StorageTier.RETAINED))`:
    different arguments and return value.
  - `get_workflow_id()` → `input.workflow_id` (needs the typed Input in
    scope); `get_workflow_run_id()` → `App.run_id`.
  - `build_output_path()` → compose the run path from `input.workflow_id`.
    The public `application_sdk.execution.build_output_path` exists, but its
    docstring forbids app code from calling it: it reads the current
    activity, so it is wrong in `run()`.
  - A deprecated error class → the typed `application_sdk.errors` subclass
    the notice names; update every `except` and `isinstance` that used it.
- **route** — a structural migration another skill owns. Record it, do not
  start it here:
  - `ParquetFileReader`, `ParquetFileWriter`, `JsonFileWriter`, other
    storage-format readers and writers, and private storage helpers such as
    `_download_files` → `migrate-storage` (`RollingFileWriter`, or a
    FileReference field on the typed Input read with `pandas.read_parquet`).
  - `QueryBasedTransformer`, `TransformerInterface` → the asset-mapper
    pattern (`migrate-asset-modeling`).
  - `BaseMetadataExtractor`, `SqlMetadataExtractor` → `templates.SqlApp`
    ([`upgrade-v3`](https://github.com/atlanhq/application-sdk/blob/main/.claude/skills/upgrade-v3/SKILL.md),
    in the `atlanhq/application-sdk` repo, not shipped with this package).
- **no public equivalent** — a private name the SDK or library does not
  expose publicly (examples seen in the fleet: `_HTTP_POOL_LIMITS`,
  `TaskContext`, `_CREDENTIAL_KEYS`, a thrift `_match_hostname` patch). Two
  options for the developer: for a plain value (a constant), copy it into
  the app under its own name; otherwise ignore it:
  `# conformance: ignore[B008] no public equivalent — tracked in <ticket id>`.
  For an SDK name, the ticket is an issue on `atlanhq/application-sdk` asking
  for a public API; never invent an id.
- **false positive** — a B001 method-name match on an object that is not
  the SDK's. Ignore it with that reason.

Order the inventory by urgency: an entry whose `removal_version` is at or
below the SDK version locked in `uv.lock` (compare as versions: `4.0` equals
`4.0.0`) can disappear in any release, even if it still exists today. Its
class does not change: a **behaviour** site still waits for the developer.

**Stop 1** (see Agent protocol).

## Step 2 — Apply

1. Apply the **swap** sites. Change the import or call only, and keep the
   surrounding logic — except where the old call was wrapped for an
   exception, as with `_resolve_store`; replace that wrapper too.
2. Apply the **behaviour** sites the developer decided, one at a time, with
   the test that covers the changed path.
3. Add the agreed ignores for **no public equivalent** and **false
   positive** sites.
4. Leave the **route** sites unchanged and list them in the hand-off.

## Step 3 — Prove it

1. `atlan-application-sdk-conformance detect --rule B001,B008` — no finding
   left except the routed sites and the agreed ignores.
2. The test suite passes, with no failure that was not in the baseline.
3. A test collects for every changed test module (`pytest --collect-only`):
   B008 changes in `tests/` break collection first.

**Stop 2** (see Agent protocol).

## Agent protocol

Two stops, developer decides at each:

1. **After Step 1** — the inventory: every site, its class, the replacement
   checked against the installed SDK, the behaviour decisions needed, and
   the routing list. No edits yet.
2. **After Step 3** — the evidence: the re-detect output, the test result,
   and the routing list for the structural sites. Then hand off for PR
   review.
