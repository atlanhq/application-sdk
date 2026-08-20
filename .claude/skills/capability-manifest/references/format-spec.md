# Format Specification — `docs/agents/sdk-capabilities.md`

This document defines the exact output format that `extractor.py render` produces. Any
deviation from this spec is a bug in the extractor. Agents reading the manifest can rely
on this structure being stable.

---

## File Header

An HTML comment block immediately at the top of the file (lines 1–7), followed by a blank line:

```
<!--
generated-by:  capability-manifest skill (.claude/skills/capability-manifest)
sdk-version:   <version>          # from application_sdk.__version__
source-sha:    <40-char hex>      # git log -1 --format=%H -- application_sdk/
source-date:   <ISO-8601>         # commit date of source-sha (NOT wall-clock time)
do-not-edit:   re-run the skill instead of hand-editing
-->
```

- `sdk-version` — from `application_sdk/version.py`.
- `source-sha` — deterministic: same source state → same SHA → byte-identical output.
- `source-date` — `git log -1 --format=%cI <source-sha>`. Tied to the SHA, not skill invocation time.
- **No wall-clock timestamps or random IDs anywhere in the file.**

**Staleness one-liner** (printed by the skill on completion; usable by any agent):

```bash
[ "$(awk '/^source-sha:/{print $2}' docs/agents/sdk-capabilities.md)" \
  = "$(git log -1 --format=%H -- application_sdk/)" ] \
  && echo "manifest current" || echo "manifest stale — run /capability-manifest"
```

---

## Section 1 — Subpackage Index

A markdown table immediately after the intro block:

```markdown
## Subpackage Index

| Subpackage | Purpose | Exports |
|---|---|---|
| `application_sdk.app` | <purpose from subpackage-purposes.yaml> | <symbol count> |
| ...                   | ...                                     | ...             |
```

- **Subpackage** — backtick-quoted full import path, alphabetical.
- **Purpose** — read from `references/subpackage-purposes.yaml` by short name (e.g., `app`).
- **Exports** — every symbol emitted for the subpackage, recounted each render. That is
  the union of the package `__init__`'s `__all__` and the `__all__` of every public
  submodule beneath it, deduplicated by name — not `len(__all__)` of the `__init__`
  alone. A subpackage can therefore appear with exports while its `__init__` declares
  no `__all__` at all (`application_sdk.server`).

---

## Section 2 — Subpackage Details

One H2 per subpackage, alphabetical. Each subpackage section has one or more H3 groups.

A subpackage section covers every public module beneath it, so the **Import** line
carries the module a symbol is actually exported from — which is not always the
subpackage root. `run_in_thread` appears under `application_sdk.execution` with
`from application_sdk.execution.heartbeat import run_in_thread`. When more than one
public module exports the same name, the shortest path wins the entry and the rest are
listed on an **Also importable from** line:

```markdown
#### `ProgressTracker`

- **Import:** `from application_sdk.execution.heartbeat import ProgressTracker`
- **Also importable from:** `application_sdk.execution.progress`
```

**Defined in** stays the file the symbol is *defined* in, which for a re-exported seam
is a private module (`application_sdk/_runtime/offload.py`). The two lines answer
different questions on purpose: **Import** is what an app writes, **Defined in** is
where to read the code.

```markdown
## `application_sdk.<name>`

<purpose line>

### Classes

#### `ClassName`

- **Import:** `from application_sdk.<name> import ClassName`
- **Signature:** `class ClassName(<first arg>, ...)`
- **Summary:** First docstring sentence.
- **Defined in:** `application_sdk/<name>/file.py`

### Decorators

#### `@decorator_name`

- **Import:** `from application_sdk.<name> import decorator_name`
- **Signature:** `decorator_name(<args>)`
- **Summary:** First docstring sentence.
- **Defined in:** `application_sdk/<name>/file.py`

### Functions

#### `function_name`
...

### Constants and Enums

#### `CONSTANT_NAME`
...
```

**Group ordering:** Classes → Decorators → Functions → Constants and Enums. Empty groups are omitted.

**Within each group:** alphabetical by symbol name (case-insensitive).

**Signature rules:**
- Full signature if ≤ 120 characters.
- Truncated to `<name>(<first arg>, ...)` if > 120 characters.
- Classes: uses `__init__` signature with `self` stripped, prefixed `class ClassName`.
- Functions/Decorators: the function's own signature string.
- Constants/Enums: `NAME: <annotation>` if annotated.

**Summary rules:**
- First line of the object's docstring.
- If no docstring: `_(no docstring)_` literally.

---

## Section 3 — Contracts

```markdown
## Contracts

Strongly-typed Inputs/Outputs for SDK methods. All inherit from
`application_sdk.contracts.base.{Input, Output}` (Pydantic).

### `application_sdk.contracts`

#### `UploadInput`

- **Import:** `from application_sdk.contracts import UploadInput`
- **Summary:** Input contract for `App.upload()` — explicit app-to-app hand-off to Atlan's upstream store.
- **Fields:**
  - `local_path: str` — local filesystem path to the file to upload
  - `tier: StorageTier` `= StorageTier.RETAINED` — controls destination prefix and cleanup policy
  - `storage_path: str | None` `= None` — override the auto-generated destination key
  - `storage_subdir: str | None` `= None` — subdirectory appended under the run prefix
  - `skip_if_exists: bool` `= False` — skip when remote SHA-256 already matches
  - `raise_on_empty: bool` `= False` — raise if the local path has no files to upload
- **Defined in:** `application_sdk/contracts/storage.py`
```

**Namespaces catalogued** (always these three, alphabetical):
1. `application_sdk.contracts`
2. `application_sdk.handler.contracts`
3. `application_sdk.templates.contracts`

**Inclusion criterion:** any class whose bases include `BaseModel`, `Input`, or `Output`.

**Field format:** `` `name: annotation` `` — for fields with non-None defaults, append `` `= <default>` ``;
for fields with a `description` attribute, append ` — <description>`.

**Field ordering:** definition order from the source file (Pydantic preserves source order).

---

## Determinism Guarantees

The render is fully deterministic given the same source state:

| Source of churn | How it's eliminated |
|---|---|
| Subpackage ordering | Alphabetical by canonical import path |
| Symbol ordering | Fixed group order; alphabetical within group |
| Field ordering | Source definition order (Pydantic stable) |
| Wall-clock timestamps | None — `source-date` is commit date of `source-sha` |
| `source-sha` | From `git log -1 -- application_sdk/`; dirty trees rejected |
| Signature truncation | Deterministic rule: ≤120 chars full, else `(first arg, ...)` |
| Docstring summary | First line of docstring or `_(no docstring)_` |
| Whitespace | Single template function; pre-commit normalises trailing spaces/EOF |

**Idempotence test:** the skill renders twice and `cmp`s. Any difference is a bug.
