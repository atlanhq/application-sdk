---
name: marketplace-packages-incremental
description: >
  Guide for creating the marketplace-packages branch and PRs to enable
  incremental extraction for a connector. Covers branching strategy
  (1 branch, 3 PRs to preprod/master/main with consistent titles),
  YAML template modifications for incremental parameters, interim app
  workflow steps, and temporary publish workarounds. Based on the Oracle
  incremental extraction PR pattern (PR #22199).
metadata:
  author: platform
  version: "1.0.0"
  category: platform
  keywords:
    - marketplace-packages
    - incremental-extraction
    - argo-workflows
    - yaml-templates
    - branching-strategy
---

# Marketplace Packages: Enable Incremental Extraction

This skill guides you through creating the marketplace-packages changes needed
to enable incremental extraction for a connector. It follows the established
pattern from Oracle's incremental extraction (PR #22199).

## When to Use This Skill

- Adding incremental extraction support to a new connector in marketplace-packages
- Modifying the Argo WorkflowTemplate YAML to pass incremental parameters
- Understanding the branching and PR strategy for marketplace-packages changes

## When NOT to Use This Skill

- Implementing the app-side incremental extraction code (use `implement-incremental-extraction` skill)
- Modifying SDK incremental logic
- Creating a brand new connector package from scratch

## Branching Strategy

### One Branch, Three PRs

Marketplace-packages uses a multi-environment deployment model:
- `main` (production) → `master` (preprod staging) → `preprod` (preprod)

**Create ONE branch** and open **THREE PRs** with the **same title structure**:

```bash
# 1. Create branch from master
git checkout master
git pull origin master
git checkout -b <ticket-id>-incremental

# 2. Make changes (see YAML Modifications below)

# 3. Create 3 PRs with consistent title:
# PR 1: <ticket-id> → preprod
gh pr create --base preprod --title "APP-XXXX - [preprod] add incremental extraction support to <connector> interim app - <description>"

# PR 2: <ticket-id> → master (Automatic Master PR)
gh pr create --base master --title "APP-XXXX - [preprod] add incremental extraction support to <connector> interim app - <description> (Automatic Master PR)"

# PR 3: <ticket-id> → main
gh pr create --base main --title "APP-XXXX - [preprod] add incremental extraction support to <connector> interim app - <description>"
```

### Title Convention

```
APP-XXXX - [preprod] add incremental extraction support to <connector> interim app - <brief description> (Automatic Master PR)
```

Example (Oracle):
```
APP-9439 - [preprod] add incremental extraction support to oracle interim app - pass entire current state instead of transformed diff to publish to avoid circuit break failures (Automatic Master PR)
```

## YAML Template Modifications

All changes go in:
```
packages/atlan/<connector>/templates/atlan-<connector>.yaml
```

### 1. Add Workflow Step References

Add the interim extraction steps to the workflow step list:

```yaml
# In the steps/configmaps section that lists valid workflow steps
"extract",
"offline-extraction",
"extract-secure-agent",
"interim-extract-and-transform",                              # NEW
"interim-extract-and-transform-app-framework-secure-agent-dag", # Existing
```

### 2. Add Incremental Parameters

Add these parameters to the connector's parameter block (alongside existing
extraction parameters):

```yaml
# Incremental extraction parameters
- name: incremental-extraction
  valueFrom:
    configMapKeyRef:
      name: atlan-tenant-package-runtime-config
      key: atlan-<connector>.main.params.incremental-extraction
      optional: true
    default: "false"
- name: column-batch-size
  value: "25000"
- name: column-chunk-size
  value: "100000"
- name: system-schema-name
  value: "SYS"  # Database-specific: SYS for Oracle, "" for ClickHouse
# Debug parameters (uncomment for debugging)
# - name: marker-timestamp
#   value: ""
```

#### Parameter Details

| Parameter | Source | Default | Purpose |
|-----------|--------|---------|---------|
| `incremental-extraction` | ConfigMap (runtime toggle) | `"false"` | Enable/disable incremental mode |
| `column-batch-size` | Hardcoded | `"25000"` | Tables per batch for column extraction |
| `column-chunk-size` | Hardcoded | `"100000"` | Column records per output chunk |
| `system-schema-name` | Hardcoded | DB-specific | System schema for metadata queries |
| `marker-timestamp` | Commented out | `""` | Debug: override marker for testing |

### 3. Update Workflow Arguments Formatting

Change `workflow-arguments` from folded (`>`) to literal (`|`) block scalar
for improved YAML readability:

```yaml
# Before
- name: workflow-arguments
  value: >
    {
      "workflow_id": "{{workflow.labels.workflows.argoproj.io/workflow-template}}"
    }

# After
- name: workflow-arguments
  value: |
    {
      "workflow_id": "{{workflow.labels.workflows.argoproj.io/workflow-template}}"
    }
```

### 4. Temporary Publish Workaround

**Important**: Currently, the publish step does NOT support publishing only
changed assets via incremental extraction. Until this is resolved, we need
a temporary workaround that passes the full current-state instead of the
transformed diff.

```yaml
# In the publish step parameters
# TEMPORARY WORKAROUND: Pass current-state instead of transformed directory
# Because publish breaks circuit breaker when receiving incremental diffs
#
# The current-state contains ALL assets (including ancestral) so publish
# treats it like a full extraction
#
# TODO: Remove this once publish supports incremental diff publishing
- name: transformed-input-path
  # OLD (standard):
  # value: "artifacts/apps/{{inputs.parameters.application-name}}/workflows/{{tasks.extraction-and-transformation.outputs.parameters.workflow_id}}/{{tasks.extraction-and-transformation.outputs.parameters.run_id}}/transformed"
  # NEW (temporary workaround):
  value: "persistent-artifacts/apps/{{inputs.parameters.application-name}}/connection/{{=sprig.last(sprig.splitList(\"/\", jsonpath(inputs.parameters.connection, '$.attributes.qualifiedName')))}}/current-state"
```

#### How the Path Works

```
Standard path:
  artifacts/apps/oracle/workflows/{workflow_id}/{run_id}/transformed

Temporary workaround path:
  persistent-artifacts/apps/oracle/connection/{connection_epoch}/current-state
```

The `sprig.last(sprig.splitList(...))` extracts the connection epoch ID from
the connection's qualified name (e.g., `default/oracle/1764230875` → `1764230875`).

## Checklist

Before submitting PRs:

- [ ] Branch created from `master`
- [ ] `interim-extract-and-transform` added to workflow steps list
- [ ] Incremental parameters added (`incremental-extraction`, `column-batch-size`, etc.)
- [ ] `workflow-arguments` uses `|` block scalar
- [ ] Publish step uses current-state path (temporary workaround)
- [ ] TODO comment added to publish workaround explaining it's temporary
- [ ] File ends with newline
- [ ] Three PRs created: preprod, master, main
- [ ] All three PRs have consistent titles
- [ ] Linear ticket linked in PR description

## PR Description Template

```markdown
## Change Summary
This pull request adds support for incremental extraction in the
<Connector> integration template, introduces new configuration parameters
for batch and chunk sizes, and temporarily adjusts the publish logic to
work around current limitations with incremental extraction.

**Incremental Extraction Support and Configuration:**
* Added new parameters to enable incremental extraction, including
  `incremental-extraction`, `column-batch-size`, and `column-chunk-size`,
  with values sourced from a config map or set to defaults.

**Workflow and Step Updates:**
* Added new workflow steps for interim app extraction.

**Temporary Workaround for Publish Logic:**
* Updated `transformed-input-path` to point to `current-state` directory
  instead of `transformed` directory, due to current limitations in
  publishing changed assets via incremental extraction.
* TODO: Remove once publish supports incremental diff publishing.

## Linear Issues Resolved
- APP-XXXX - <link>
```

## Reference PRs

- Oracle: [PR #22199](https://github.com/atlanhq/marketplace-packages/pull/22199)
  - Branch: `app-9439-incremental`
  - File: `packages/atlan/oracle/templates/atlan-oracle.yaml`
  - Changes: +30 lines, -3 lines
