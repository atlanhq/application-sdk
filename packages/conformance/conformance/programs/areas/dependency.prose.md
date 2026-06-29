---
kind: responsibility
name: dependency-area
description: >
  Maintains the current D-series violation-set and drives remediation of
  pyproject.toml dependency-contract findings.  Mechanical fixes (add an SDK
  upper bound, delete a redeclared line, raise the requires-python floor) are
  applied automatically; the bare-SDK-pin case is proposed and routed to
  residue for human review.
---

### Maintains

The current set of unsuppressed D-series (dependency-contract) conformance
findings in the working tree, as reported by `suite.runner --series D`.

#### violations-dependency

The fingerprint-set of all unsuppressed FAILING D-series results.  Extends to
include WARNING results in strict mode — D002/D003/D004/D005/D006/D007/D008 are
WARN-tier, so they are processed in strict mode; D001 is BLOCK-tier and
processed in both modes.

This facet's fingerprint moves when any D-series finding is resolved (fixed or
suppressed with justification) or when new ones appear.  An unchanged
fingerprint-set across loop iterations is the oscillation signal.

Postcondition (deterministic validator — never render-attested):

> `atlan-application-sdk-conformance detect --repo . --series D` exits 0
> (zero unsuppressed FAILING results) after all remediable findings are
> processed.  In strict mode, additionally: the `atlan/summary.warning` count
> in the SARIF output is 0 (zero unsuppressed WARNING results).

### The re-detection gate is authoritative for this area

D-series edits change `pyproject.toml` text, **not** the installed
environment: the loop does not `uv sync` between the edit and the gates.  The
orthogonal gate (`uv run … pytest`) therefore runs against the *unchanged*
resolved env, so it can neither break (no wrongful revert) nor validate a
dependency edit — a green orthogonal result here is vacuous and must not be
read as confirming the fix.  The protective gate for D is
`recheck-narrowest`, which re-runs `suite.runner --series D` scoped to the
touched `pyproject.toml`; the detector reads the file directly, so the edit is
reflected and the finding's fingerprint genuinely disappears only when the
text is correct.  Treat D edits like suppression-only edits with respect to
trust in the test gate: rely on re-detection.

**No gate validates the D001 cap *value*, only its presence.** `D001`'s
detector (`_is_bounded_specifier`) tests only that an upper bound exists — a
wrong cap such as `<3.0.0` that *excludes* the installed major still clears the
finding and ships a broken pyproject.  The D001 prescription in this file's **Fix Prescription** section (cap at the
next major so the range includes the pinned version) is therefore the sole
safeguard for cap correctness; the remediator must follow it exactly, and the
human review of residue is the
backstop.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.

### Continuity

Input-driven: re-render when `pyproject.toml` under `scope` changes.  In the
Claude Code skill path the skill caller drives re-invocation on demand rather
than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "D"
  mode: mode
  max_attempts: 5
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "dependency"`._

Consult the finding's `hint` and `message`, then read the actual lines around
`finding.line` in `finding.file` (always `pyproject.toml`) before proposing a
fix.  The re-detection gate is authoritative for this area — see
**The re-detection gate is authoritative for this area** above.

**Mechanical rules** (`classification = "mechanical"`, `outcome = "fix"`):

- **D001 UnpinnedSdkDependency** (`classification = "mechanical"`) — the
  `atlan-application-sdk` entry in `[project.dependencies]` has a lower bound
  but no upper bound (e.g. `"atlan-application-sdk>=3.17"` or
  `"atlan-application-sdk[sql]>=2.3.1"`).  Add an upper bound at
  **(lower-bound major + 1).0.0** so the cap *includes* the pinned version and
  stops the next major: read the major from the existing lower bound
  (`>=3.x` → major 3 → `,<4.0.0`) and append it to the specifier, preserving
  any `[extras]`.
  `"atlan-application-sdk[sql]>=3.17"` → `"atlan-application-sdk[sql]>=3.17,<4.0.0"`.

  **Bare-pin exception** (`classification = "judgment"`): if the entry has no
  version at all (`"atlan-application-sdk"` or `"atlan-application-sdk[workflows]"`),
  there is no major to infer — the correct supported range is a human call.
  Route to residue with a proposed `">=<current>,<<next-major>.0.0"` for the
  maintainer to confirm.

- **D002 RedeclaredSdkManagedDependency** — the named package is already
  pinned by the SDK and redeclared in the app's `[project.dependencies]` or a
  `[project.optional-dependencies.*]` array.  Delete the entire redeclaring
  line (the SDK installs it transitively).  Remove only that one entry; leave
  the rest of the array intact.  If the deletion empties the array, leave the
  empty array rather than removing the table.

- **D004 RedeclaredSdkManagedDependencyInGroups** — same as D002 but the
  redeclaration is in a `[dependency-groups.*]` table.  Delete the entire
  redeclaring line; if the SDK-managed package was dev/test tooling the app
  genuinely needs, prefer pulling it in via `atlan-application-sdk[tests]`
  instead (note that as a follow-up in the edit description).  Remove only that
  one entry.

- **D006 IncompatibleRequiresPython** — the app's `[project].requires-python`
  lower bound is below the SDK's floor.  Replace only the lower-bound clause
  with the SDK floor named in the finding message (`>=3.11`), preserving any
  upper bound: `requires-python = ">=3.10,<4.0"` → `">=3.11,<4.0"`;
  `requires-python = ">=3.10"` → `">=3.11"`.

- **D007 NonStandardBuildBackend** — set `build-backend` to `"hatchling.build"`
  in `[build-system]` and ensure `requires = ["hatchling"]`.  Build-affecting:
  state in the edit description that the app must `uv lock` / rebuild and that a
  human should confirm no backend-specific config (e.g. setuptools
  `[tool.setuptools]` tables) is left orphaned.

- **D008 WeakenedTypeChecking** — raise `[tool.pyright].typeCheckingMode` to
  `"standard"` (leave `"strict"` untouched; only `"off"`/`"basic"` are flagged).
  Replace only the mode value.

**Advisory rules** (`autofixable = false`, `classification = "judgment"`;
WARN-tier — route to residue for human decision):

- **D003 UnusedDependency** — a package declared in `[project.dependencies]`
  (or a `[project.optional-dependencies.*]` / `[dependency-groups.*]` array) is
  not imported anywhere in the scanned Python source.  The finding message names
  the package.  This is intentionally advisory: a dependency can be loaded
  dynamically, via an entry-point/plugin, or invoked as a server binary (e.g.
  `uvicorn`) without an explicit import.  Draft one of:

  1. **Remove the entry** (preferred if the package is genuinely unused): delete
     the line.  State in the edit description that you verified no dynamic load
     or entry-point registration references the package.
  2. **Move to the correct group**: if the package is test/dev tooling that
     belongs in `[dependency-groups.dev]` or `[dependency-groups.test]`, propose
     moving it there rather than removing it.
  3. **Suppress**: if the package is intentionally runtime-loaded (plugin,
     optional backend, server process), propose a `# conformance: ignore[D003]
     <reason>` comment on the entry line and explain the load mechanism.

  Never auto-delete without reading the codebase context — dynamic imports,
  `__import__`, `importlib.import_module`, entry-point declarations in
  `[project.entry-points.*]`, and `console_scripts` are all legitimate uses.

**Judgment rules** (`autofixable = false`, `classification = "judgment"`; route
to residue):

- **D005 UnknownSdkExtra** — the `atlan-application-sdk[<extra>]` reference
  names an extra the SDK does not publish.  Propose the closest published extra
  if the name is an obvious typo or a renamed/removed extra (read the SDK's
  `Provides-Extra` set from the finding context); otherwise propose removing the
  bogus extra.  Mapping a dropped extra to its replacement requires judgement,
  so always route to residue for human confirmation.

**Suppress outcome (strict mode only, WARNING-tier findings)**:

When `mode == "strict"` and `finding.disposition == "warning"`, the model may
propose an inline suppression instead of a fix if the deviation is a deliberate,
justified exception for this app.  Applicable rules and notes:

- **D002 / D004 / D005 / D006 / D007 / D008** — standard inline suppression.
  TOML uses `#` for comments, so the directive trails the entry or sits on the
  line above it:

  ```
  "pyyaml>=6.0,<7",  # conformance: ignore[D002] <concise justification, 8–40 words>
  ```

- **D003** — the suppress path is **option 3 in the D003 advisory section
  above** (an inline `# conformance: ignore[D003] <reason>` on the entry line
  explaining the dynamic-load mechanism).  Use that path rather than the
  generic suppress outcome; it produces the same directive with the required
  load-mechanism justification.

The justification must describe *why* the deviation is acceptable here, not
merely that the rule is being suppressed.  Route every suppression to residue
for human audit.  D001 is BLOCK-tier and has no suppress path in default mode.
