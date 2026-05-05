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
| `application_sdk.app` | <purpose from subpackage-purposes.yaml> | <len(__all__)> |
| ...                   | ...                                     | ...            |
```

- **Subpackage** — backtick-quoted full import path, alphabetical.
- **Purpose** — read from `references/subpackage-purposes.yaml` by short name (e.g., `app`).
- **Exports** — `len(__all__)` recomputed each render from the `__init__.py` AST.

---

## Section 2 — Subpackage Details

One H2 per subpackage, alphabetical. Each subpackage section has one or more H3 groups.

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
- **Summary:** Input contract for ObjectStore.upload.
- **Fields:**
  - `source: str` — local path
  - `key: str` — destination object-store key
  - `tier: StorageTier` `= StorageTier.HOT` — storage tier
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
