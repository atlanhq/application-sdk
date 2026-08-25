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
include WARNING results in strict mode — D002/D003/D004/D006/D007/D008 are
WARN-tier, so they are processed in strict mode; D001, D005, D009, D010 and
D011 are BLOCK-tier and processed in both modes.

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

**`D011` validates the specifier's *shape*, not its *values*.**  The detector
rejects pins and one-sided ranges, so a bare name or `==0.13.0` cannot slip
through as `D001`'s wrong cap can.  What it does **not** check is whether the
floor is sensible: rewriting `>=0.17.0` to `>=0.1.0,<1.0.0` clears the finding
while admitting ancient releases.  The prescription below (keep the app's floor
when it is higher; never lower one) is the only safeguard for that, and human
review of residue is the backstop.

The lock branch (branch 4) is also the one D-series finding whose fix is
**not** a `pyproject.toml` edit — it needs `uv lock`.  The re-detection gate
reads the lock for that branch, so an edit that does not run `uv lock` will not
clear it.

### Requires

- `scope` — repository root path.
- `mode` — `"default"` or `"strict"`.
- `rule_ids` — optional list of exact rule IDs (propagated from the
  top-level entry). Forwarded verbatim into every runner invocation this
  area makes — the loop's detect calls and the suggest-only
  `detect-violations` calls alike — so a `--rule`-scoped run stays scoped
  here rather than silently widening to the whole series at this hop.

### Continuity

Input-driven: re-render when `pyproject.toml` under `scope` changes.  In the
Claude Code skill path the skill caller drives re-invocation on demand rather
than watching the filesystem.

### Execution

```prose
call detect-fix-recheck
  scope: scope
  series: "D"
  rule_ids: rule_ids
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

- **D011 ConformanceDependencyContract** (`classification = "mechanical"` for
  branches 1, 2 and 3) — BLOCK-tier, no suppress path in default mode, same as
  D001.  One rule, four branches; read the message to tell them apart, because
  the fix differs.  The canonical target in
  all four cases is
  `"atlan-application-sdk-conformance>=0.17.0,<1.0.0"` in
  `[dependency-groups].dev`, matching `atlan-app-template`.

  1. *"does not declare"* — the package appears in no dependency array, so
     `uv run atlan-application-sdk-conformance` (the form this loop and the
     bootstrapped `remediate` skill use) fails to spawn.  Add the canonical
     entry; create the `[dependency-groups]` table with a `dev` array if the
     file has none.  `finding.line` anchors at the `[dependency-groups]`
     header, or line 1 when there is no such table.  **Never** add it to
     `[project.dependencies]` — that ships a dev-only tool in the runtime
     image, and branch 2 below will then fire on your own edit.
  2. *"is declared in `[project.dependencies]`"* — a placement violation, not
     a spawn one: the script does run, so nothing else fires.  **Move** the
     entry — delete the `[project.dependencies]` line and add the canonical
     entry to `[dependency-groups].dev` (or to the dev group the repo already
     uses) — do not leave the runtime line in place beside a new dev-group
     entry, which is the failure mode this branch exists to catch.  If a
     correct dev-group entry already exists, deleting the runtime line is the
     whole fix.  `finding.line` anchors at the offending
     `[project.dependencies]` entry.
  3. *"cannot float"* — declared, but the specifier is a pin (`==0.13.0`,
     `===0.13.0`, `~=0.17.0`) or one-sided (`>=0.17.0` with no upper bound, or
     a bare `<1.0.0` with no floor).  Rewrite **that entry only** to the
     canonical two-sided form, preserving its position in the array.  Keep the
     app's existing floor if it is higher than `0.17.0`; never lower a floor.
     `finding.line` anchors at the declaring line.
  4. *"no entry in uv.lock"* — declared and floating, but unresolved in the
     lock.  This one is **not** a `pyproject.toml` edit: run `uv lock` and
     commit the result.  Classify it `judgment` and route to residue if the
     loop cannot run `uv lock` in its environment — a hand-edited `uv.lock` is
     never an acceptable fix.

  For branches 1, 2 and 3, note in the edit description that `uv lock` must be
  re-run so the change reaches the lockfile: the re-detection gate reads
  `pyproject.toml` for those branches and will pass without it, and branch 4
  then becomes the next finding.

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

- **D009 RemoteDaprComponentFetch** — `finding.line` points at the offending
  URL; find the enclosing `[tool.poe.tasks.*]` entry (whichever form it's
  written in — `task.shell = "..."` shorthand or a full
  `[tool.poe.tasks.task]` table) — it fetches Dapr component YAMLs from
  `raw.githubusercontent.com` or the GitHub contents API for
  `atlanhq/application-sdk`. Replace that task's body with a local copy from
  the installed wheel, preserving the task's name:

  ```toml
  [tool.poe.tasks]
  download-components.shell = """
  python -c "
  import application_sdk, pathlib, shutil
  src = pathlib.Path(application_sdk.__file__).parent / 'components'
  shutil.copytree(src, 'components', dirs_exist_ok=True)
  "
  """
  ```

  Preserve the task's existing name; only its body changes. This is BLOCK-tier
  and has no suppress path in default mode, same as D001.

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

- **D010 QueryTransformerWithoutDuckdb** — the app imports the SDK query
  transformer (`application_sdk.transformers.query`; the finding message names
  the import site) but `duckdb` does not resolve: it is missing from `uv.lock`
  (or, with no lock, no `atlan-application-sdk[sql]`/`[incremental]` extra or
  direct `duckdb` dependency is declared).  Every `transform_metadata` call
  then fails at runtime with `ImportError: duckdb is required for
  DuckDBConnectionManager` — latent breakage that imports and mocked tests
  never exercise (fleet SDR sweep).  The finding is anchored at the SDK
  dependency line in `pyproject.toml`.

  **Check the extra on that line before proposing anything.**  If the app pins
  `atlan-application-sdk[daft]`, the defect is the SDK's, not the app's: that
  extra resolved to nothing over SDK 3.22–3.27 and aliases `[sql]` again from
  3.28.0.  Propose raising the SDK floor to `>=3.28.0` and leave the extra
  alone — the bump is the whole fix, and rewriting the app's extras instead
  pays per repo for a defect fixed once upstream.

  Otherwise (a plain SDK pin with no extra — the shape the rule now describes),
  draft the edit that changes the SDK reference to `atlan-application-sdk[sql]`
  (or `[incremental]` for the incremental analytics stack).  Do **not** propose
  declaring `duckdb` directly as an equivalent option: it clears the finding
  but duplicates a pin the SDK's extras manage, leaving the app to track the
  SDK's range by hand.  Either way note that `uv lock` must be re-run; the
  relock touches the resolved environment, so route to residue rather than
  auto-applying (the D-series loop does not `uv sync` between edit and gates).

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

- **D002 / D004 / D005 / D006 / D007 / D008 / D010** — standard inline
  suppression.
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
for human audit.  D001, D009 and D011 are BLOCK-tier and have no suppress path in
default mode.
