# /upgrade-v3 Skill Gaps

> **Source:** Production debugging of AlloyDB Postgres connector upgrade (2026-04-21 to 2026-04-23)
> **Skill location:** `.claude/skills/upgrade-v3/SKILL.md`
> **Checker location:** `tools/migrate_v3/check_migration.py`
> **Migration prompt:** `tools/migrate_v3/MIGRATION_PROMPT.md`

---

## Status Legend

- **OPEN** — not addressed in skill or checker
- **PARTIAL** — mentioned in SKILL.md gotchas but no automated check
- **DONE** — automated checker rule exists

---

## 1. Checker Gaps (rules that should exist but don't)

### 1.1 Manifest `task_queue` validation
**Status:** OPEN
**Severity:** CRITICAL — causes workflow to hang with "No Workers Running"

The checker does not validate `contract/generated/manifest.json` at all. It should:
- Parse the manifest JSON and validate syntax
- Check that `task_queue` values match SDK conventions:
  - Extract step: `atlan-{ATLAN_APPLICATION_NAME}-{deployment_name}`
  - Publish step: `atlan-publish-{deployment_name}`
- Flag `publish-queue`, `{app_name}-queue`, or other non-standard queue names
- Distinguish `{app_name}` (SDK service name, e.g. `alloy-db-postgres-app`) from `ATLAN_APPLICATION_NAME` (e.g. `alloydb-postgres`)

**Production failure:** Publish workflow hung indefinitely because manifest used `publish-queue` instead of `atlan-publish-{deployment_name}`.

### 1.2 Manifest publish step required args
**Status:** OPEN
**Severity:** HIGH — causes publish to skip steps silently

The publish step in `manifest.json` requires these args:
```json
{
  "connection_qualified_name": "$.extract.outputs.connection_qualified_name",
  "transformed_data_prefix": "...",
  "publish_state_prefix": "...",
  "current_state_prefix": "...",
  "connection_creation_enabled": true,
  "executor_enabled": true,
  "connection_entity": "{{connection}}",
  "connection_cache_enabled": true,
  "connection_cache_via_app_enabled": true,
  "current_state_via_app_enabled": true
}
```

Missing any of these causes silent failures — no error logs, just 0 entities published. The checker should have a `manifest-publish-args` rule.

### 1.3 `run()` override missing `upload_to_atlan` / `connection_qualified_name`
**Status:** OPEN
**Severity:** HIGH — causes 0 entities in publish

When a connector overrides `run()`, it loses the base template's `upload_to_atlan` call and `connection_qualified_name` extraction. Add checker rules:
- `run-override-missing-upload` (WARN): detect `async def run(self, ...)` without `upload_to_atlan(` in same file
- `run-override-missing-connection-qn` (WARN): detect `async def run(self, ...)` without `connection_qualified_name =` in same file

Regex patterns:
```python
_RE_RUN_OVERRIDE = re.compile(r"^\s+async\s+def\s+run\s*\(\s*self\b", re.MULTILINE)
_RE_UPLOAD_TO_ATLAN = re.compile(r"\bupload_to_atlan\s*\(")
_RE_CONNECTION_QN_SET = re.compile(r"\bconnection_qualified_name\s*=")
```

### 1.4 `ruff-format` corrupting JSON files
**Status:** OPEN
**Severity:** MEDIUM — causes invalid JSON in manifest

The checker should inspect `.pre-commit-config.yaml` and warn if `ruff-format` hook is missing `types: [python]`. Without it, ruff adds trailing commas to JSON files, breaking manifest parsing.

**Production failure:** `manifest.json` was corrupted by ruff-format adding trailing commas.

### 1.5 `ObjectStore.upload_prefix()` after transform
**Status:** OPEN
**Severity:** CRITICAL — causes publish to find 0 entities

Local disk writes are invisible to publish-app (separate pod, reads from S3). The checker should warn when a connector writes transform output to disk but doesn't call `upload_prefix()` or `upload_to_atlan()`. Currently completely undetected.

**Production failure:** Transform wrote JSONL locally but never uploaded — publish completed with 0 entities and no errors.

---

## 2. Codemod Gaps (mechanical transforms the skill should automate)

### 2.1 SQL connector `prepare_query()` metadata dict
**Status:** OPEN

The `_build_workflow_args()` method needs a `metadata` dict with **hyphenated keys** for `prepare_query()`:
```python
metadata = {
    "include-filter": input.include_filter,
    "exclude-filter": input.exclude_filter,
    "temp-table-regex": input.temp_table_regex,
}
```

This is a mechanical pattern for every SQL connector. The skill should generate it automatically via codemod rather than leaving it to AI inference.

### 2.2 AE hyphenated payload deserialization
**Status:** OPEN (SDK-level bug, skill should detect)

AE sends hyphenated keys (`credential-guid`, `include-filter`) but Pydantic `Input` expects underscored (`credential_guid`, `include_filter`). The SDK `Input` base class needs `alias_generator` or `model_validator` to handle this. Until fixed, the skill should:
- Test that Input models can deserialize AE-format payloads
- Add a checker rule `input-hyphenated-alias` that warns if Input subclasses don't handle kebab-case keys

### 2.3 `entity_types` over `run()` override
**Status:** OPEN

When the only reason to override `run()` is to add extra entity types (procedures, views), the skill should use the declarative `entity_types` class attribute instead:
```python
class MyApp(SqlMetadataExtractor):
    entity_types = ["Table", "View", "Column", "Procedure"]
```

This keeps the base template's upload and connection_qualified_name logic intact. The skill should detect this pattern and prefer `entity_types` over a full `run()` override.

---

## 3. Documentation / Guidance Gaps

### 3.1 Manifest must be in deployed image
**Status:** OPEN

`contract/generated/manifest.json` is baked into the Docker image at build time. Updating it in the repo without rebuilding/redeploying means the server still serves the old manifest. The skill should remind users to redeploy after manifest changes.

The Phase 5 summary report should include:
```
REMINDER: Rebuild and redeploy the Docker image for manifest changes to take effect.
```

### 3.2 Queue naming convention documentation
**Status:** PARTIAL (mentioned in gotchas but not systematically)

Three different naming conventions exist:
| Context | Pattern | Example |
|---------|---------|---------|
| Helm `_helpers.tpl` | `{appName}-queue` | `alloydb-postgres-queue` |
| SDK `_derive_task_queue()` | `atlan-{ATLAN_APPLICATION_NAME}-{deployment_name}` | `atlan-alloydb-postgres-production` |
| Publish app | `atlan-publish-{deployment_name}` | `atlan-publish-production` |

The skill should document this table and ensure manifest uses the SDK convention, not the Helm convention.

### 3.3 `connection_qualified_name` extraction pattern
**Status:** PARTIAL (mentioned in SKILL.md gotcha but no codemod)

Every SQL connector must extract `connection_qualified_name` from the input:
```python
connection_qn = input.connection.get("attributes", {}).get("qualifiedName", "")
# or for typed contracts:
connection_qn = input.connection.attributes.qualified_name
```

And set it on the output:
```python
output.connection_qualified_name = connection_qn
```

The `SqlMetadataExtractor` base template should handle this automatically. If it doesn't, the skill should inject it.

---

## 4. Already Addressed (for reference)

These items from the original debugging are already covered:

| Item | Coverage |
|------|----------|
| Handler discovery | SKILL.md gotcha (line 797-806) |
| Asset-mapper vs Transformer | SKILL.md gotcha (line 808-850) |
| `fetch_metadata` return types | SKILL.md gotcha (line 852-861) + checker WARN |
| Preflight auto-conversion | SKILL.md gotcha (line 863-871) |
| Credentials format | SKILL.md gotcha (line 873-891) |
| `allow_unbounded_fields` | SKILL.md gotcha (line 893-905) + checker FAIL |
| Dockerfile pattern | SKILL.md gotcha (line 907-943) |
| `output_path` computation | SKILL.md gotcha (line 945-957) |
| pre-commit exclude generated | SKILL.md gotcha (line 959-970) |
| Multi-workflow consolidation | SKILL.md gotcha (line 990-1039) |
| `self.context` vs `self.task_context` | SKILL.md gotcha (line 1041-1043) |
| `os.environ` in sandbox | SKILL.md gotcha (line 1142-1153) |
| Deprecated imports | Checker FAIL rule `no-deprecated-imports` |
| v2 decorators | Checker FAIL rule `no-v2-decorators` |
| `BaseApplication()` | Checker FAIL rule `no-base-application` |
| Direct DaprClient | Checker FAIL rule `no-dapr-client` |
| Direct temporalio imports | Checker FAIL rule `no-temporalio-direct-import` |

---

## Priority Order for Implementation

1. **1.5** `upload_prefix()` detection — CRITICAL, silent failure
2. **1.1** Manifest task_queue validation — CRITICAL, workflow hangs
3. **1.2** Manifest publish args validation — HIGH, silent failure
4. **1.3** `run()` override checks — HIGH, silent failure
5. **2.2** AE hyphenated payload handling — HIGH, SDK bug
6. **1.4** ruff-format JSON corruption — MEDIUM, build-time
7. **2.1** `prepare_query()` metadata codemod — MEDIUM, mechanical
8. **2.3** `entity_types` preference — MEDIUM, code quality
9. **3.1** Redeploy reminder — LOW, documentation
10. **3.2** Queue naming docs — LOW, documentation
11. **3.3** `connection_qualified_name` pattern — LOW, should be in base template
