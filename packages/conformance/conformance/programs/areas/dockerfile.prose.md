---
kind: responsibility
name: dockerfile-area
description: >
  Maintains the current I-series violation-set and drives SUGGEST-ONLY
  remediation of Dockerfile conformance findings.  Mechanical fixes (base image
  swap, CMD/ENTRYPOINT deletion, mode-env deletion, USER root deletion) are
  drafted for human review; I003 (missing ATLAN_APP_MODULE) is a judgment draft.
  All I-series rules are BLOCK-tier — no suppress path exists.
---

### Maintains

The current set of unsuppressed I-series (Dockerfile) conformance findings in
the working tree, as reported by `suite.runner --series I`.

#### violations-dockerfile

The fingerprint-set of all unsuppressed FAILING I-series results in the current
working tree.  All I-series rules are BLOCK-tier, so this facet is non-empty
only when a Dockerfile violation is present; there are no WARNING-tier findings
and no strict-mode extension.

This facet's fingerprint moves when any I-series finding is resolved (fixed or
the Dockerfile is corrected by a human acting on a drafted proposal).  An
unchanged fingerprint-set across loop iterations is the oscillation signal.

Postcondition (suggest-only — the loop proposes but does not apply):

> Every I-series finding routes to the residue report with a drafted edit
> attached.  The working tree is left unchanged by this area; a human reviews
> each proposal and applies it (or rejects it) manually.

**Why suggest-only:** Dockerfile edits carry structural side-effects that
behavioural tests cannot fully cover (layer ordering, entrypoint interactions,
build-time vs. run-time env separation).  The safe form is propose-don't-apply
until a Dockerfile linting gate is wired in.

### Requires

- `scope` — repository root path (provided by the top-level responsibility at
  expansion time).
- `mode` — `"default"` or `"strict"` (propagated from the top-level entry).
  Strict mode has no additional effect here (all I-series rules are BLOCK-tier).

### Continuity

Input-driven: re-render this node when `Dockerfile` under `scope` changes.
In the Claude Code skill path the skill caller re-invokes on demand.

### Execution

```prose
# Suggest-only: detect, draft a fix per finding, route to residue WITHOUT
# applying.  All I-series rules are BLOCK-tier; no suppress path exists.
let violations = call detect-violations
  scope: scope
  series: "I"
  target: "failing"

for each finding in violations:
  let proposal = call remediate-finding
    finding: finding
    mode: mode

  add { finding, proposal } to residue with note "I-series suggest-only: proposed Dockerfile edit drafted for human review; NOT applied (no Dockerfile gate validates structural changes)"
```

### Fix Prescription

_Read by `remediate-finding` when `finding.area == "dockerfile"`._

Read the full Dockerfile context before proposing any edit; changes to one
instruction may interact with others.

**Mechanical rules** (`autofixable = true`, `classification = "mechanical"`):

- **I001 DockerfileWrongBaseImage** — depends on the shape of the `FROM` line:

  - **Literal base** (`FROM <wrong-image>`) — replace the final `FROM` line
    with the exact approved image:

    ```
    FROM registry.atlan.com/public/app-runtime-base:3
    ```

    Preserve any `AS <alias>` suffix if present (uncommon in single-stage
    Dockerfiles; drop it otherwise).  This is a single-line substitution with a
    known constant — no judgment needed.

  - **Build-arg base** (`FROM ${BASE_IMAGE}` / `$BASE_IMAGE`) — do **not**
    replace the `FROM` line with a literal; that would destroy the deliberate
    override mechanism (CI rebuilds on a PR-scoped base via `--build-arg`).
    Instead fix the `ARG` default so it pins the approved image:

    ```
    ARG BASE_IMAGE=registry.atlan.com/public/app-runtime-base:3
    ```

    If no `ARG` for the referenced variable exists, add one with that default
    before the `FROM` line.  Leave the `FROM ${BASE_IMAGE}` line unchanged.

- **I002 DockerfileEntrypointOverride** — delete the `CMD` or `ENTRYPOINT`
  line.  The base image's entrypoint script handles launch and graceful drain;
  the line serves no purpose once the app is wired via `ATLAN_APP_MODULE`.  If
  the removed instruction was the app's only start command, note in residue
  that `ENV ATLAN_APP_MODULE` must also be present (I003 will flag it
  independently if not).

- **I004 DockerfileAppModeHardcoded** — delete the `ENV ATLAN_APP_MODE=…`
  line.  No replacement is needed; the variable is supplied at deploy time by
  the deployment manifest.

- **I005 DockerfileRootUser** — delete the `USER root` or `USER 0` line.  The
  base image already runs as `appuser`; the line is always wrong in the final
  stage.  If a `RUN` step immediately follows that depends on root access
  (e.g. `apt-get install`), note in residue that the install must move to an
  earlier build stage — do **not** add a compensating `USER appuser` to hide
  the `USER root`.

**Judgment rules** (`autofixable = false`, `classification = "judgment"`):

- **I003 DockerfileAppModuleMissing** — no `ENV ATLAN_APP_MODULE=…` is set.
  Draft an addition after the `FROM` line (or after the last `ENV` block):

  ```
  ENV ATLAN_APP_MODULE=<module>:<AppClass>
  ```

  Do **not** invent a module path; leave `<module>:<AppClass>` as a literal
  placeholder and instruct the developer to substitute the real value.  Only
  the developer knows the correct path — this cannot be inferred statically.

**Suppress outcome** is not available for I-series (all rules are BLOCK-tier).
