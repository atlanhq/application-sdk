---
name: toolkit-feature-workflow
description: Use when reviewing an application-sdk PR that changes contract-toolkit behavior, generated contract artifacts, toolkit examples, or toolkit docs. Runs toolkit-only PR review mode: classify affected generated surfaces, perform mandatory private downstream compatibility validation through Mothership/Rover, and post sanitized review findings without exposing internal consumer repo paths.
---

# Toolkit PR Review Workflow

Use this skill only in PR review mode. It does not implement toolkit features.

The contract toolkit lives under `contract-toolkit/` in the SDK repository. It
renders app setup config, credential config, Automation Engine manifests, typed
SDK input models, and bundle metadata. A PR can pass local Pkl checks while still
breaking a downstream consumer, so every substantive review must validate the
affected generated surface against the relevant consumer class.

## Review Contract

1. Read the PR, diff, changed files, and `contract-toolkit/AGENTS.md`.
2. Classify the changed surface:
   - workflow/setup UI: `Config.pkl`, `Widgets.pkl`, setup widgets/properties.
   - credentials: `FieldSpec`, `AuthOption`, `ConditionalFieldSpec`, credential config output.
   - manifest: node args, `{{params}}`, static values, output references.
   - DAG/default nodes: typed nodes, node ids, dependencies, task queues, workflow/activity labels.
   - generated SDK input: `_input.py` names, defaults, types, `ExtractionInput` collisions.
   - bundle/root metadata: `NativeAppBundle.pkl`, generated `atlan.yaml`, shared credentials.
   - examples/docs/release metadata: generated output, docs, changelog, PR title.
3. Run only relevant validations, but treat them as mandatory once the surface is
   affected. Do not approve over an unrun mandatory check.
4. Distinguish compatibility from adoption:
   - Optional new fields do not need every downstream app to already use them.
   - Required generated values, changed placeholders, changed node args, or changed
     runtime assumptions must be validated against the relevant consumer.
   - If the PR claims compatibility with a specific app pattern, validate that
     representative pattern.
5. Keep private evidence private. Internal repo names, file paths, and clone
   locations may be used in scratch notes, but public PR comments must use
   capability names such as "UI rendering", "manifest substitution", "workflow
   execution", and "representative app pattern".

## Mandatory Local Checks

Run these from the SDK repo root when the relevant files changed:

```bash
contract-toolkit/scripts/regenerate-all.sh
contract-toolkit/scripts/check-invariants.sh
(cd contract-toolkit && pkl test tests/*.pkl)
uv run --extra workflows python contract-toolkit/scripts/test-sdk-import.py
git diff --check
```

Inspect generated output whenever `contract-toolkit/src/` or examples changed:

```bash
git diff -- contract-toolkit/examples
find contract-toolkit/examples -name "_input.py" -print
```

Do not hand-edit generated files to satisfy stale-output findings; require
regeneration.

## Mandatory Private Consumer Validation

Use `.mothership/pr-review/references/toolkit-consumer-registry.md` as the
private registry. Mothership/Rover should clone missing consumers into a scratch
directory or use an existing checkout, fetch the configured branch, and record
the checked SHA privately.

Maintain `/tmp/TOOLKIT_VALIDATION.md` as the private validation ledger. Each
mandatory capability must be recorded once with one of `validated`, `not
applicable`, or `needs rerun`. Any `needs rerun` status prevents approval.

Validation mapping:

- UI/setup or credential shape changed -> validate UI rendering consumers.
- Manifest placeholders, static args, or parameter names changed -> validate
  manifest substitution consumers.
- DAG/default typed node behavior changed -> validate workflow execution consumers.
- Generated `_input.py` shape changed -> validate SDK runtime input import and
  any representative app pattern the PR targets.
- App-specific node/field changed -> validate the representative app pattern only
  when the PR changes required behavior, generated output, or claims that app's
  compatibility.

If a mandatory private check cannot run because Rover/Mothership infrastructure
failed, do not expose the internal failure details. Add a final public note:

```text
Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge.
```

## Public Review Format

Post a concise review with these sections:

```text
### Affected Toolkit Surfaces
- <surface>: <why it is affected>

### Compatibility Checks
- UI rendering compatibility: validated | not applicable | needs rerun
- Manifest substitution compatibility: validated | not applicable | needs rerun
- Workflow execution contract: validated | not applicable | needs rerun
- Generated SDK input contract: validated | not applicable | needs rerun
- Representative app pattern: validated | not applicable | needs rerun

### Findings
<file-grouped findings, sanitized>

### Review Note
<only if a mandatory private check could not complete due to Rover execution>
```

Public findings can mention generated artifact names such as `manifest.json`,
`_input.py`, or workflow/credential config, but must not mention private consumer
repo paths or internal app file paths.

Before posting, run `.mothership/pr-review/scripts/redact-toolkit-public-review.sh`
on the summary and inline comment bodies.

## Verdict Rules

- Compatibility break found -> `NEEDS_FIXES`.
- Required generated output stale -> `NEEDS_FIXES`.
- Missing tests/docs/examples for a public toolkit behavior change -> `NEEDS_FIXES`.
- Mandatory consumer validation blocked by Rover/Mothership execution -> no
  approval; request rerun or human review using the sanitized note.
- Optional field not yet adopted by a downstream app -> not a finding.
- Mixed `application_sdk/**` and `contract-toolkit/**` PR -> require both SDK
  review and toolkit review tracks before approval.
