# YAML Template Reference

This reference shows the exact YAML modifications needed based on the Oracle PR #22199.

## Full Diff (Oracle Example)

The changes below show what was modified in
`packages/atlan/oracle/templates/atlan-oracle.yaml`:

### Change 1: Add workflow step

```yaml
# Location: spec.templates[].steps (workflow steps list)
# Add "interim-extract-and-transform" to the list of valid steps

"extract",
"offline-extraction",
"extract-secure-agent",
"interim-extract-and-transform",                              # ← ADD THIS
"interim-extract-and-transform-app-framework-secure-agent-dag",
"publish.publish.publish.publish-prod.transform",
```

### Change 2: Add incremental parameters

```yaml
# Location: spec.templates[].container.env (connector parameters)
# Add after existing extraction parameters (e.g., use-jdbc-internal-methods)

          - name: use-jdbc-internal-methods
            value: "true"

          # Incremental extraction parameters
          - name: incremental-extraction
            valueFrom:
              configMapKeyRef:
                name: atlan-tenant-package-runtime-config
                key: atlan-oracle.main.params.incremental-extraction
                optional: true
              default: "false"
          - name: column-batch-size
            value: "25000"
          - name: column-chunk-size
            value: "100000"
          - name: system-schema-name
            value: "SYS"
          # Debug parameters (uncomment for debugging)
          # - name: marker-timestamp
          #   value: ""
```

### Change 3: Workflow arguments formatting

```yaml
# Location: spec.templates[].arguments.parameters
# Change > (folded) to | (literal) for workflow-arguments

# Before:
                - name: workflow-arguments
                  value: >
                    {
                      "workflow_id": "..."
                    }

# After:
                - name: workflow-arguments
                  value: |
                    {
                      "workflow_id": "..."
                    }
```

### Change 4: Publish path workaround

```yaml
# Location: spec.templates[].steps (publish step arguments)

              parameters:
                # Comment explaining the temporary workaround
                # Currently publish does not support publishing changed assets
                # via incremental extraction, hence the logic fails in calculate
                # diff as it breaks the circuit breaker.
                # Therefore, temporarily we are passing the current-state directory
                # instead of transformed directory which has the context of all
                # assets including ancestral runs.
                # TODO: Remove this once publish supports publishing changed
                # assets via incremental extraction
                - name: transformed-input-path
                  value: "persistent-artifacts/apps/{{inputs.parameters.application-name}}/connection/{{=sprig.last(sprig.splitList(\"/\", jsonpath(inputs.parameters.connection, '$.attributes.qualifiedName')))}}/current-state"
```

## ConfigMap Key Convention

The ConfigMap key follows the pattern:
```
atlan-<connector>.main.params.<parameter-name>
```

Examples:
- `atlan-oracle.main.params.incremental-extraction`
- `atlan-clickhouse.main.params.incremental-extraction`
- `atlan-postgresql.main.params.incremental-extraction`

The ConfigMap `atlan-tenant-package-runtime-config` is a tenant-level config
that can be toggled at runtime without redeploying the workflow.

## Connector-Specific Values

| Parameter | Oracle | ClickHouse | PostgreSQL |
|-----------|--------|------------|------------|
| ConfigMap key prefix | `atlan-oracle` | `atlan-clickhouse` | `atlan-postgresql` |
| `system-schema-name` | `SYS` | _(not needed)_ | `information_schema` |
| `column-batch-size` | `25000` | `25000` | `25000` |
| `column-chunk-size` | `100000` | `100000` | `100000` |

## Sprig Template Expression

The publish path workaround uses a Sprig template expression:

```
{{=sprig.last(sprig.splitList("/", jsonpath(inputs.parameters.connection, '$.attributes.qualifiedName')))}}
```

This extracts the connection epoch ID:
1. `jsonpath(...)` gets `$.attributes.qualifiedName` → `"default/oracle/1764230875"`
2. `sprig.splitList("/", ...)` splits by `/` → `["default", "oracle", "1764230875"]`
3. `sprig.last(...)` gets last element → `"1764230875"`

Result: `persistent-artifacts/apps/oracle/connection/1764230875/current-state`

## Validation Checklist

After making changes, verify:

```bash
# Validate YAML syntax
python -c "import yaml; yaml.safe_load(open('packages/atlan/<connector>/templates/atlan-<connector>.yaml'))"

# Check for trailing whitespace
grep -n ' $' packages/atlan/<connector>/templates/atlan-<connector>.yaml

# Ensure file ends with newline
test -n "$(tail -c1 packages/atlan/<connector>/templates/atlan-<connector>.yaml)" && echo "Missing trailing newline"
```
