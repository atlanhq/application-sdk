# Contract Toolkit Consumer Registry

This file is private reviewer context. Use it to decide which downstream
compatibility checks are mandatory for a contract-toolkit PR. Do not quote
repository names, internal paths, clone locations, or file paths from this file
in public PR comments. Public review text must use capability aliases only.

## Public Aliases

Use these aliases in review comments:

- `UI rendering compatibility`
- `Manifest substitution compatibility`
- `Workflow execution contract`
- `Generated SDK input contract`
- `Representative app pattern`

## Core Consumers

Clone missing repos under `/tmp/toolkit-review-consumers/<repo>`. If a sibling
checkout already exists under `/workspace`, use it. Always fetch the configured
remote branch and record the SHA privately before validating.

Every consumer check must be PR-bound. It is not enough to clone/fetch the
consumer repository. The check must evaluate generated artifacts from the PR
checkout or a scratch copy of a consumer contract rewritten to amend/import
`/workspace/application-sdk/contract-toolkit/src/*.pkl`.

| Capability alias | Repository | Branch | Purpose |
|---|---|---|---|
| UI rendering compatibility | `atlanhq/atlan-frontend` | `origin/beta` | Workflow and credential config rendering, widget support, conditional/default behavior. |
| UI rendering compatibility | `atlanhq/blaze` | `origin/main` | Playground/schema rendering and manifest payload substitution previews. |
| Manifest substitution compatibility | `atlanhq/heracles` | `origin/beta` | Manifest `{{param}}` substitution, unresolved placeholder behavior, credential/config propagation. |
| Workflow execution contract | `atlanhq/atlan-automation-engine-app` | `origin/main` | DAG node ids, dependencies, labels, task queues, workflow types, args, and error semantics. |

## Representative App Patterns

Use app-pattern checks only when a PR changes a required generated value,
changes runtime assumptions, or claims compatibility for that app pattern. An
optional field that no app has adopted yet is not a failure by itself.

| Pattern | Trigger terms | Repository | Branch | Public alias |
|---|---|---|---|---|
| query intelligence | `QueryIntelligenceNode`, `query intelligence`, `QI`, `query_intelligence` | `atlanhq/atlan-query-intelligence-app` | `origin/main` | Representative app pattern |
| publish | `PublishNode`, `publish`, `publish controls`, `current state` | `atlanhq/atlan-publish-app` | `origin/main` | Representative app pattern |
| popularity | `PopularityNode`, `popularity` | `atlanhq/atlan-popularity-app` | `origin/main` | Representative app pattern |
| lineage | `LineageNode`, `LineagePublishNode`, `lineage` | `atlanhq/atlan-lineage-app` | `origin/main` | Representative app pattern |

If a trigger term matches multiple patterns, validate each relevant pattern
unless the diff or PR body clearly narrows the claim.

## Surface Mapping

| Changed surface | Mandatory validations |
|---|---|
| `contract-toolkit/src/Config.pkl` or `contract-toolkit/src/Widgets.pkl` | UI rendering compatibility |
| Credential fields in `NativeApp.pkl`, `Credential.pkl`, or examples | UI rendering compatibility |
| `manifest.json` rendering, node args, placeholders, static values, output refs | Manifest substitution compatibility |
| Typed nodes, DAG defaults, dependencies, labels, task queues, workflow/activity names | Workflow execution contract |
| Generated `_input.py`, field names, defaults, aliases, or SDK import behavior | Generated SDK input contract |
| `NativeAppBundle.pkl`, generated root `atlan.yaml`, bundle shared credentials | Manifest substitution compatibility, workflow execution contract |
| PR claims a system-app/default-node compatibility story | Representative app pattern |

## Minimum Actionable Checks

Do not mark a capability as `validated` unless at least one PR-bound command or
inspection below completed and produced evidence.

Write the result of every mandatory capability to `/tmp/TOOLKIT_VALIDATION.md`
using the public alias and one of: `validated`, `not applicable`, `needs rerun`.
The public PR summary may include these status names, but not the private
repository, path, or SHA evidence.

| Capability alias | PR-bound input | Minimum check | Failure means |
|---|---|---|---|
| UI rendering compatibility | Changed/generated workflow or credential JSON from this PR | Run the consumer's relevant schema/parser/unit test when available, or inspect the exact generated keys against widget/conditional/default handling in the fetched consumer SHA. | Generated config uses a widget, rule, default, or nesting shape the UI consumer does not handle. |
| Manifest substitution compatibility | Changed/generated `manifest.json` from this PR | Inspect `{{params}}`, static args, and output refs against substitution behavior in the fetched consumer SHA; run targeted tests when present. | Placeholder cannot be produced, is stripped incorrectly, or changes required propagation semantics. |
| Workflow execution contract | Changed/generated `manifest.json` from this PR | Inspect node ids, deps, workflow/activity labels, task queues, workflow types, and args against execution models in the fetched consumer SHA; run targeted tests when present. | Generated DAG cannot be accepted or executed with the current execution contract. |
| Generated SDK input contract | Changed/generated `_input.py` from this PR | Run `uv run --extra workflows python contract-toolkit/scripts/test-sdk-import.py` and inspect field names/types/defaults for runtime collisions. | Generated Python cannot import or runtime input fields no longer match generated config/manifest expectations. |
| Representative app pattern | Scratch copy of the representative app contract rewritten to use `/workspace/application-sdk/contract-toolkit/src/*.pkl`, or generated PR artifact that represents the pattern | Regenerate or inspect the app-pattern contract against the PR toolkit source. If no contract exists yet, validate only the generic generated shape and record adoption as not applicable. | PR claims or requires app-pattern compatibility that current representative contract/runtime cannot satisfy. |

## Validation Failure Handling

- Compatibility break found: public finding with sanitized capability alias,
  verdict `NEEDS_FIXES`.
- Mandatory validation cannot run because Rover/Mothership clone/auth/network
  failed: do not disclose internal details. Add the public note:
  `Review note: one required compatibility check could not be completed due to a Rover execution issue. Please re-run @sdk-review or request human review before merge.`
- Mandatory validation cannot run because examples/generated artifacts are
  missing from the PR: public finding, verdict `NEEDS_FIXES`.
- Optional field not adopted by an app yet: not a finding unless the PR claims
  adoption or changes required runtime behavior.
