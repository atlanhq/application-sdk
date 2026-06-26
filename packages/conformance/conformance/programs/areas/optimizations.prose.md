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

### Continuity

Input-driven: re-render this node when any `*.py` file under `scope` changes.
This is the Reactor-ready wake source — in the Claude Code skill path, the
skill caller re-invokes on demand rather than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "O"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "optimizations"`._

Consult the finding's `hint` and `message`, then look at the actual source
lines around `finding.line` in `finding.file` before proposing a fix.

**Judgment rules** (`autofixable = false`) — produce a `"fix"` outcome with
`classification = "judgment"`; always route to residue:

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
    options); a `default=` callable stays as the `default` keyword (orjson
    supports it).  Drop kwargs orjson cannot express and note them in residue.
  - Ensure `import orjson` is present at module top (it is a core SDK
    dependency); add it if missing.

  The orthogonal gate **bites** here: a `bytes`/`str` regression on any
  covered path fails the behavioural tests, so a careless swap is caught by
  `orthogonal-gate` before the edit survives.  Classification is always
  `"judgment"` (the decode/kwargs call requires reading the call site), so the
  edit is also routed to residue for human confirmation.

- **O002 LegacyAssetSerialization** (asset-mapper, BLDX-1492) — an asset is
  serialized with the pydantic `.dict()` method in a module that imports pyatlan
  asset models.  The asset-mapper transform task writes assets with the v9
  serialization API — `out_f.write(asset.to_nested_bytes() + b"\n")` — which emits
  the nested-entity wire shape the platform ingests; `.dict()` produces a flat
  dict that still needs hand-conversion.  Draft the switch to
  `asset.to_nested_bytes()` (note it returns `bytes`, so the sink must be a
  bytes/JSONL writer).  If the flagged `.dict()` is on a **non-asset** pydantic
  model, propose an inline `# conformance: ignore[O002] <reason>` instead.

- **O003 UntypedAssetMapperReturn** (asset-mapper, BLDX-1492) — a function builds
  a pyatlan asset and returns it but declares no return annotation.  Draft the
  smallest mechanical fix: add `-> <Asset>` naming the asset class the function
  constructs (e.g. a function building and returning a `Table` becomes
  `def map_table(...) -> Table:`).  If the function legitimately returns a union
  or `Optional`, annotate accordingly.  Classification is `"judgment"` only
  because the author may intend a wider return type; the edit is otherwise
  mechanical.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and the site legitimately needs stdlib `json` (e.g.
interop with a library that requires a `str` and the bytes-decode round-trip
is wasteful, or a `json.JSONEncoder` subclass), the model may propose an
inline `# conformance: ignore[O001] <justification>` instead of a fix.  Route
every suppression to residue for human audit.
